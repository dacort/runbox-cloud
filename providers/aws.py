import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TypeVar, Union

import boto3
import click
import yaml
from botocore.exceptions import ClientError

from providers import Provider

logger = logging.getLogger(__name__)

# Type hints
T = TypeVar("T", bound="Resource")

# Global environment configuration
_current_environment = "default"


def set_environment(env: str):
    """Set the current environment."""
    global _current_environment
    _current_environment = env
    # Reset StateManager singleton to use new environment
    StateManager._instance = None


def get_environment() -> str:
    """Get the current environment."""
    return _current_environment


class ResourceState(Enum):
    """Possible states of a resource."""

    PENDING = "pending"
    CREATING = "creating"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"


class StateManager:
    """Manages resource state persistence using YAML."""

    _instance = None

    def __new__(cls, state_file: str = "cloud_resources.yaml"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, state_file: str = "cloud_resources.yaml"):
        if hasattr(self, "_initialized"):
            return

        self.state_file = Path(state_file)
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()
        self._initialized = True

    def _load_state(self):
        """Load state from YAML file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self._state = yaml.safe_load(f) or {}
            except (yaml.YAMLError, FileNotFoundError):
                self._state = {}

    def save_state(self):
        """Save state to YAML file."""
        with open(self.state_file, "w") as f:
            yaml.dump(self._state, f, default_flow_style=False, indent=2)

    def get_resource_config(self, resource_key: str) -> Optional[Dict[str, Any]]:
        """Get configuration for a resource in the current environment."""
        env = get_environment()
        return self._state.get(env, {}).get(resource_key)

    def set_resource_config(self, resource_key: str, config: Dict[str, Any]):
        """Set configuration for a resource in the current environment."""
        env = get_environment()
        if env not in self._state:
            self._state[env] = {}
        self._state[env][resource_key] = config
        self.save_state()

    def remove_resource(self, resource_key: str):
        """Remove a resource from state in the current environment."""
        env = get_environment()
        if env in self._state and resource_key in self._state[env]:
            del self._state[env][resource_key]
            self.save_state()


def depends_on(*resources):
    """Class decorator to declare resource dependencies."""

    def decorator(cls):
        cls.depends_on = list(resources)
        return cls

    return decorator


class Resource(ABC):
    """A resource for a provider, like an S3 bucket or EC2 instance.

    Resources can depend on other resources, like EC2 depends on VPC.
    The dependent resource can be provided as an argument:
      `EC2Instance(vpc=vpc_instance)`
    If not provided, it will create a new instance of the dependency with defaults.

    Certain resources can also be retained by default, like VPC.
    These resources get written to the state file and reused in future runs.
    """

    # Class attributes
    provider: str = "generic"
    resource_type: str = "unknown"
    retain_by_default: bool = False

    def __init__(
        self, retain: Optional[bool] = None, name: Optional[str] = None, **kwargs
    ):
        self.retain = retain if retain is not None else self.retain_by_default
        self.name = name  # Optional name for the resource instance, required if multiple instances of the same resource type are created
        self._dependents: List["Resource"] = []
        self._config: Dict[str, Any] = {}
        self._state_manager = StateManager()

        # Handle dependencies
        required = getattr(self.__class__, "depends_on", [])
        for dep_cls in required:
            dep_name = dep_cls.__name__.lower()
            dep_instance = kwargs.get(dep_name)
            if dep_instance is not None:
                if not isinstance(dep_instance, dep_cls):
                    raise ValueError(
                        f"Dependency '{dep_name}' must be an instance of {dep_cls.__name__}."
                    )
            else:
                dep_instance = dep_cls()
            setattr(self, dep_name, dep_instance)
            self._dependents.append(dep_instance)

        # Load existing config if available
        self._load_config()

    @property
    def resource_key(self) -> str:
        """Unique key for this resource in the state file."""
        key = f"{self.provider}:{self.resource_type}:{self.__class__.__name__}"
        if self.name:
            key += f":{self.name}"
        return key

    def _load_config(self):
        """Load configuration from state manager."""
        # TODO: This probably isn't the right approach, but it works for now
        if self.retain:
            config = self._state_manager.get_resource_config(self.resource_key)
            if config and config.get("state") != ResourceState.DELETED.value:
                self._config = config
                print(
                    f"Loaded existing config for {self.__class__.__name__}: {self._config.get('resource_id', 'unknown')}"
                )

    def _save_config(self):
        """Save configuration to state manager."""
        if self.retain and self._config:
            self._state_manager.set_resource_config(self.resource_key, self._config)

    def get_or_create(self) -> "Resource":
        """Get or create the resource instance."""
        # Check if we have existing config and resource exists
        if self._config and self._config.get("resource_id"):
            if self._exists():
                print(
                    f"Using existing {self.__class__.__name__}: {self._config['resource_id']}"
                )
                return self

        # Ensure dependents are created first
        for dep in self._dependents:
            dep.get_or_create()

        # Create this resource
        print(f"Creating {self.__class__.__name__}...")
        self._config = {
            "state": ResourceState.CREATING.value,
            "created_at": datetime.now().isoformat(),
            "resource_id": None,
        }

        try:
            resource_id = self._create()
            self._config["resource_id"] = resource_id
            self._config["state"] = ResourceState.ACTIVE.value
            self._save_config()
            print(f"Created {self.__class__.__name__}: {resource_id}")
        except Exception as e:
            self._config["state"] = ResourceState.ERROR.value
            self._save_config()
            raise e

        return self

    def destroy(self):
        """Destroy the resource."""
        if not self._config or not self._config.get("resource_id"):
            return

        print(f"Destroying {self.__class__.__name__}: {self._config['resource_id']}")
        self._config["state"] = ResourceState.DELETING.value
        self._save_config()

        try:
            self._destroy()
            self._config["state"] = ResourceState.DELETED.value
            if not self.retain:
                self._state_manager.remove_resource(self.resource_key)
            else:
                self._save_config()
        except Exception as e:
            self._config["state"] = ResourceState.ERROR.value
            self._save_config()
            raise e

    @abstractmethod
    def _create(self) -> str:
        """Create the resource instance. Returns resource ID."""
        pass

    @abstractmethod
    def _destroy(self):
        """Destroy the resource instance."""
        pass

    @abstractmethod
    def _exists(self) -> bool:
        """Check if the resource exists."""
        pass


class AWS(Provider):
    def __init__(self):
        pass

    def run(self, executable, instance_type, options=None):
        click.echo(f"Running {executable} on AWS with instance type {instance_type}")


# AWS Resources
class AWSResource(Resource):
    """Base class for AWS resources."""

    provider = "aws"


class VPC(AWSResource):
    """Create a public VPC with an S3 gateway, internet gateway, and default security group.

    The subnet does not have public IPs by default, but can be assigned per instance.
    The default security group allows all outbound traffic, but no inbound traffic.

    No NAT gateway is used to keep costs low.
    """

    resource_type = "network"
    retain_by_default = True

    def __init__(self, cidr_block: str = "10.120.0.0/16", **kwargs):
        self.cidr_block = cidr_block
        super().__init__(**kwargs)

    def _create(self) -> str:
        ec2 = boto3.client("ec2")

        # Create VPC
        vpc_response = ec2.create_vpc(
            CidrBlock=self.cidr_block,
            TagSpecifications=[
                {
                    "ResourceType": "vpc",
                    "Tags": [
                        {"Key": "Name", "Value": self.name or "cloudrun-preview-vpc"},
                        {"Key": "cloudrun-version", "Value": "v0.0.1"},
                    ],
                }
            ],
        )
        vpc_id = vpc_response["Vpc"]["VpcId"]

        # Enable DNS support
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

        # TODO: Quick validation to make sure the VPC was created
        # TODO: Create an S3 endpoint - they're free, so why not?
        # But right now the EC2 instance profile doesn't have any access to S3.

        # Create internet gateway
        igw_response = ec2.create_internet_gateway()
        igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        # Create subnet
        subnet_response = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.120.1.0/24")
        subnet_id = subnet_response["Subnet"]["SubnetId"]

        # Create route tables to allow internet access
        route_table_response = ec2.create_route_table(VpcId=vpc_id)
        route_table_id = route_table_response["RouteTable"]["RouteTableId"]
        ec2.create_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=igw_id,
        )
        ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)

        # Create security group
        sg_response = ec2.create_security_group(
            GroupName=f"cloudrun-{vpc_id}",
            Description="Default security group",
            VpcId=vpc_id,
        )
        sg_id = sg_response["GroupId"]

        # Update config with all VPC details
        self._config.update(
            {
                "vpc_id": vpc_id,
                "security_group_id": sg_id,
                "subnet_ids": [subnet_id],
                "internet_gateway_id": igw_id,
                "cidr_block": self.cidr_block,
            }
        )

        return vpc_id

    def _destroy(self):
        ec2 = boto3.client("ec2")

        # Detach and delete internet gateway
        if self._config.get("internet_gateway_id"):
            ec2.detach_internet_gateway(
                InternetGatewayId=self._config["internet_gateway_id"],
                VpcId=self._config["vpc_id"],
            )
            ec2.delete_internet_gateway(
                InternetGatewayId=self._config["internet_gateway_id"]
            )
            self._config.pop("internet_gateway_id")

        # Delete subnets
        for subnet_id in self._config.get("subnet_ids", []):
            ec2.delete_subnet(SubnetId=subnet_id)
            self._config["subnet_ids"].remove(subnet_id)

        # Delete security group
        if self._config.get("security_group_id"):
            ec2.delete_security_group(GroupId=self._config["security_group_id"])
            self._config.pop("security_group_id")

        # Delete route tables
        route_tables = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [self._config["vpc_id"]]},
                # {"Name": "association.main", "Values": ["true"]},
            ]
        )
        for rt in route_tables.get("RouteTables", []):
            if any([a.get("Main") for a in rt.get("Associations", [])]):
                continue
            logger.info(f"Deleting route table {rt['RouteTableId']}")
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])

        # Delete VPC
        ec2.delete_vpc(VpcId=self._config["vpc_id"])

    def _exists(self) -> bool:
        if not self._config.get("vpc_id"):
            return False

        ec2 = boto3.client("ec2")
        try:
            response = ec2.describe_vpcs(VpcIds=[self._config["vpc_id"]])
            return len(response["Vpcs"]) > 0
        except:
            return False


class AWSEC2InstanceType:
    """A simple class to represent an AWS EC2 instance type."""

    def __init__(self, type_name: str):
        # TODO: Add validation for instance type names
        self.type_name = type_name

    def __str__(self):
        return self.type_name

    def al2023_ssm(self) -> str:
        # User data script to install SSM agent
        user_data_script = """#!/bin/bash
        dnf install -y amazon-ssm-agent
        systemctl enable amazon-ssm-agent
        systemctl start amazon-ssm-agent
        """

        return user_data_script


class EC2IAMrole(AWSResource):
    """A simple class to represent an AWS IAM role for EC2 instances.

    This role specifically adds the `AmazonSSMManagedInstanceCore` policy to allow
    for easy remote connectivity.

    Additional permissions can be added via the command-line.
    """

    resource_type = "iam"
    retain_by_default = True
    role_name: str

    def __init__(self, role_name: Optional[str] = None, **kwargs):
        if role_name:
            self.role_name = role_name
        else:
            # TODO: I _don't_ think this is gonna work, but we'll try it!
            self.role_name = (
                f"cloudrun-ec2-role-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )

        super().__init__(**kwargs)

    def _create(self) -> str:
        iam = boto3.client("iam")

        # Create IAM role
        response = iam.create_role(
            RoleName=self.role_name,
            Path="/cloudrun/",
            Description="CloudRun EC2 role",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            Tags=[
                {"Key": "cloudrun-version", "Value": "v0.0.1"},
            ],
        )
        role_arn = response["Role"]["Arn"]
        self._config["role_arn"] = role_arn
        self._config["role_name"] = self.role_name

        # Attach policy to allow SSM access
        iam.attach_role_policy(
            RoleName=self.role_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )

        return role_arn

    def _exists(self) -> bool:
        if not self._config.get("role_name"):
            return False
        role_name = self._config.get("role_name")

        iam = boto3.client("iam")
        try:
            response = iam.get_role(RoleName=role_name)
            return "Role" in response and response["Role"]["RoleName"] == role_name
        except iam.exceptions.NoSuchEntityException:
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking IAM role: {e}")
            return False

    def _destroy(self):
        if not self._config.get("role_name"):
            return False
        role_name = self._config.get("role_name")

        iam = boto3.client("iam")

        # Detach policy
        iam.detach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )

        iam.delete_role(RoleName=role_name)


@depends_on(EC2IAMrole)
class EC2InstanceProfile(AWSResource):
    """A simple class to represent an AWS EC2 instance profile for the IAM role."""

    resource_type = "iam"
    retain_by_default = True
    ec2iamrole: Optional[EC2IAMrole]

    def __init__(self, ec2iamrole: Optional[EC2IAMrole] = None, **kwargs):
        self.ec2iamrole = ec2iamrole
        super().__init__(ec2iamrole=ec2iamrole, **kwargs)

    def _create(self) -> str:
        # TODO: See if there's a better way to do this - this _should_ always be present
        if not self.ec2iamrole:
            raise ValueError("EC2IAMrole is required but not provided")

        iam = boto3.client("iam")

        # Create instance profile
        response = iam.create_instance_profile(
            InstanceProfileName=self.ec2iamrole.role_name,
            Path="/cloudrun/",
            Tags=[{"Key": "cloudrun-version", "Value": "v0.0.1"}],
        )
        iam.add_role_to_instance_profile(
            InstanceProfileName=self.ec2iamrole.role_name,
            RoleName=self.ec2iamrole.role_name,
        )

        instance_profile_arn = response["InstanceProfile"]["Arn"]
        self._config["instance_profile_arn"] = instance_profile_arn

        return instance_profile_arn

    def _exists(self) -> bool:
        if not self._config.get("instance_profile_arn"):
            return False

        iam = boto3.client("iam")
        role_config = self.ec2iamrole._config
        role_name = role_config.get("role_name")

        try:
            response = iam.get_instance_profile(InstanceProfileName=role_name)
            return (
                "InstanceProfile" in response
                and response["InstanceProfile"]["InstanceProfileName"] == role_name
            )
        except iam.exceptions.NoSuchEntityException:
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking instance profile: {e}")
            return False

    def _destroy(self):
        if not self._config.get("instance_profile_arn"):
            return

        if not self.ec2iamrole:
            raise ValueError("EC2IAMrole is required but not provided")

        role_config = self.ec2iamrole._config
        role_name = role_config.get("role_name")

        iam = boto3.client("iam")
        iam.remove_role_from_instance_profile(
            InstanceProfileName=role_name, RoleName=role_name
        )
        iam.delete_instance_profile(InstanceProfileName=role_name)


@depends_on(VPC, EC2InstanceProfile)
class EC2Instance(AWSResource):
    resource_type = "compute"
    vpc: VPC
    ec2instanceprofile: Optional[EC2InstanceProfile]
    use_public_ip: bool = True

    def __init__(
        self,
        instance_type: Union[AWSEC2InstanceType, str],
        ami_id: Optional[str] = None,
        vpc: Optional[VPC] = None,
        ec2instanceprofile: Optional[EC2InstanceProfile] = None,
        **kwargs,
    ):
        if isinstance(instance_type, str):
            instance_type = AWSEC2InstanceType(instance_type)
        self.instance_type = instance_type
        self.ami_id = ami_id or self._get_default_ami()
        super().__init__(vpc=vpc, ec2instanceprofile=ec2instanceprofile, **kwargs)

    def _get_default_ami(self) -> str:
        """Get default AMI for the instance type."""
        # TODO: Move this into the instance type class
        # This is a simplified example and only supports al2023 on x86 or arm64
        ssm_parameter = "al2023-ami-minimal-kernel-default-x86_64"
        if re.search(r"\dg\w+\.", self.instance_type.type_name):
            ssm_parameter = "al2023-ami-minimal-kernel-default-arm64"

        return f"resolve:ssm:/aws/service/ami-amazon-linux-latest/{ssm_parameter}"

    def _create(self) -> str:
        ec2 = boto3.client("ec2")

        # Get VPC config
        vpc_config = self.vpc._config
        if not vpc_config.get("security_group_id"):
            raise ValueError("VPC configuration is not available")

        # Build up the instance config
        run_instance_kwargs = {
            "InstanceType": self.instance_type.type_name,
            "ImageId": self.ami_id,
            "MinCount": 1,
            "MaxCount": 1,
            "IamInstanceProfile": {
                "Arn": self.ec2instanceprofile._config["instance_profile_arn"]
            }
            if self.ec2instanceprofile
            else None,
            "UserData": self.instance_type.al2023_ssm(),
        }

        if self.use_public_ip:
            run_instance_kwargs["NetworkInterfaces"] = [
                {
                    "AssociatePublicIpAddress": True,
                    "DeviceIndex": 0,
                    "SubnetId": vpc_config["subnet_ids"][0],
                    "Groups": [vpc_config["security_group_id"]],
                }
            ]
        else:
            run_instance_kwargs["SecurityGroupIds"] = [vpc_config["security_group_id"]]
            run_instance_kwargs["SubnetId"] = vpc_config["subnet_ids"][0]

        # Retry logic for instance profile propagation
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = ec2.run_instances(**run_instance_kwargs)
                break  # Success, exit retry loop
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                error_message = e.response.get("Error", {}).get("Message", "")
                # Check if this is the IAM instance profile propagation error
                if (
                    error_code == "InvalidParameterValue"
                    and "Invalid IAM Instance Profile ARN" in error_message
                ):
                    if attempt < max_retries - 1:  # Don't sleep on the last attempt
                        time.sleep(2)
                        continue

                # Re-raise the exception if it's not our target error or we've exhausted retries
                logger.error(f"Error creating EC2 instance: {error_message}")
                raise

        instance_id = response["Instances"][0]["InstanceId"]

        # Update config
        self._config.update(
            {
                "instance_id": instance_id,
                "instance_type": self.instance_type.type_name,
                "ami_id": self.ami_id,
            }
        )

        return instance_id

    def _destroy(self):
        if not self._config.get("instance_id"):
            return

        ec2 = boto3.client("ec2")
        ec2.terminate_instances(InstanceIds=[self._config["instance_id"]])

    def _exists(self) -> bool:
        if not self._config.get("instance_id"):
            return False

        ec2 = boto3.client("ec2")
        try:
            response = ec2.describe_instances(InstanceIds=[self._config["instance_id"]])
            instances = []
            for reservation in response["Reservations"]:
                instances.extend(reservation["Instances"])

            return len(instances) > 0 and instances[0]["State"]["Name"] != "terminated"
        except:
            return False

    def run_command(self, command: str, timeout_seconds: int = 300) -> dict:
        """Run a shell command on the instance via AWS-RunShellScript.
        
        Returns a dictionary with:
        - status: 'Success', 'Failed', 'Cancelled', 'TimedOut'
        - stdout: command output (up to 24KB)
        - stderr: command errors (up to 8KB)  
        - exit_code: command exit code
        """
        if not self._config.get("instance_id"):
            raise ValueError("Instance is not created; call get_or_create() first")

        instance_id = self._config["instance_id"]
        
        # Ensure the instance is registered with SSM before running command
        self._wait_for_ssm_agent(instance_id, timeout_seconds)
        
        ssm = boto3.client("ssm")
        
        # Execute via SSM RunShellScript document
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout_seconds,
        )
        
        command_id = response["Command"]["CommandId"]
        
        # Wait for completion and get output
        return self._wait_for_command_completion(instance_id, command_id)

    def _wait_for_command_completion(self, instance_id: str, command_id: str) -> dict:
        """Wait for SSM command to complete and return results."""
        import time
        ssm = boto3.client("ssm")
        
        while True:
            try:
                response = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                
                status = response["Status"]
                
                if status in ["Success", "Failed", "Cancelled", "TimedOut"]:
                    return {
                        "status": status,
                        "stdout": response.get("StandardOutputContent", ""),
                        "stderr": response.get("StandardErrorContent", ""),
                        "exit_code": response.get("ResponseCode", -1),
                    }
                
                time.sleep(2)
                
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvocationDoesNotExist":
                    time.sleep(2)
                    continue
                raise

    def _should_use_interactive_mode(self, command: str) -> bool:
        """Determine if command should use interactive mode based on characteristics."""
        command_lower = command.lower().strip()
        
        # Shell invocations - these should always be interactive
        shell_commands = [
            'shell',      # Generic shell request
            'bash',       # Bash shell
            '/bin/bash',  # Full path bash
            'zsh',        # Zsh shell
            '/bin/zsh',   # Full path zsh
            'sh',         # Generic shell
            '/bin/sh',    # Full path sh
            'fish',       # Fish shell
            '/usr/bin/fish', # Full path fish
        ]
        
        # Check if the command is exactly a shell command or starts with one
        for shell in shell_commands:
            if command_lower == shell or command_lower.startswith(shell + ' '):
                return True
        
        # Also check for some other interactive commands that benefit from real-time I/O
        interactive_patterns = [
            'tail -f',    # Log following
            'watch',      # Periodic updates  
            'top',        # Interactive monitoring
            'htop',       # Interactive monitoring
        ]
        
        for pattern in interactive_patterns:
            if pattern in command_lower:
                return True
            
        return False

    def run(self, command: str, timeout_seconds: int = 300, mode: str = "auto") -> int:
        """Run a shell command on the instance with intelligent mode selection.

        Args:
            command: Shell command to execute
            timeout_seconds: Maximum execution time
            mode: Execution mode - 'auto', 'interactive', 'batch'
                - 'auto': Choose based on command characteristics  
                - 'interactive': Use session manager plugin for real-time I/O
                - 'batch': Use AWS-RunShellScript for simple execution

        Returns:
            Exit code of the command
        """
        # Determine execution mode
        if mode == "auto":
            use_interactive = self._should_use_interactive_mode(command)
        elif mode == "interactive":
            use_interactive = True
        elif mode == "batch":
            use_interactive = False
        else:
            raise ValueError(f"Invalid mode '{mode}'. Use 'auto', 'interactive', or 'batch'")
        
        if use_interactive:
            # Handle special case of generic "shell" command
            if command.lower().strip() == "shell":
                command = "bash"  # Default to bash for generic shell request
                print("Starting interactive shell session...")
            else:
                print(f"Running command interactively: {command}")
            return self._run_interactive(command, timeout_seconds)
        else:
            print(f"Running command in batch mode: {command}")
            result = self.run_command(command, timeout_seconds)
            
            # Print output for batch mode
            if result["stdout"]:
                print(result["stdout"], end="")
            if result["stderr"]:
                import sys
                print(result["stderr"], file=sys.stderr, end="")
            
            # Return exit code (convert to int if needed)
            exit_code = result["exit_code"]
            return int(exit_code) if exit_code != -1 else 1

    def _run_interactive(self, command: str, timeout_seconds: int = 300) -> int:
        """Run a shell command using the session manager plugin for real-time I/O."""
        import asyncio
        import sys

        if not self._config.get("instance_id"):
            raise ValueError("Instance is not created; call get_or_create() first")

        instance_id = self._config["instance_id"]

        # Ensure the instance is registered with SSM before starting a session
        self._wait_for_ssm_agent(instance_id, timeout_seconds)

        try:
            import boto3  # re-import local for mypy friendliness
            from session_manager_plugin.cli.types import ConnectArguments
            from session_manager_plugin.communicator.utils import create_websocket_config
            from session_manager_plugin.communicator.data_channel import (
                SessionDataChannel,
            )
            from session_manager_plugin.session.session_handler import SessionHandler
            from session_manager_plugin.session.plugins import StandardStreamPlugin
            from session_manager_plugin.session.registry import get_session_registry
        except Exception as e:
            raise RuntimeError(
                "Session Manager Plugin import failed. Ensure the dependency is installed: "
                "`uv sync` with pyproject declaring python-session-manager-plugin, or install the package."
            ) from e

        # Start an SSM session to obtain StreamUrl/TokenValue
        ssm = boto3.client("ssm")
        start = ssm.start_session(Target=instance_id)

        # Wire up a programmatic session using the plugin primitives
        args = ConnectArguments(
            session_id=start["SessionId"],
            stream_url=start["StreamUrl"],
            token_value=start["TokenValue"],
            target=instance_id,
            session_type="Standard_Stream",
        )

        # Register the Standard_Stream plugin (avoid duplicate warnings)
        registry = get_session_registry()
        try:
            if not registry.is_session_type_supported("Standard_Stream"):
                registry.register_plugin("Standard_Stream", StandardStreamPlugin())
        except Exception:
            registry.register_plugin("Standard_Stream", StandardStreamPlugin())

        handler = SessionHandler()

        # Enhanced command execution with better exit code detection
        async def _run_and_wait() -> int:
            exit_code = 0
            
            # Create session (without starting)
            session = await handler.validate_input_and_create_session(
                {
                    "sessionId": args.session_id,
                    "streamUrl": args.stream_url,
                    "tokenValue": args.token_value,
                    "target": args.target,
                    "sessionType": args.session_type,
                }
            )

            # Build data channel and handlers
            ws_config = create_websocket_config(args.stream_url, args.token_value)
            data_channel = SessionDataChannel(ws_config)

            # Capture output to detect exit code
            output_buffer = []
            
            def on_remote_input(data: bytes) -> None:
                try:
                    # Buffer output for exit code detection
                    output_buffer.append(data)
                    # Stream to stdout
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except Exception:
                    pass

            loop = asyncio.get_running_loop()
            closed_async = asyncio.Event()

            def on_closed() -> None:
                try:
                    loop.call_soon_threadsafe(closed_async.set)
                except Exception:
                    pass

            data_channel.set_input_handler(on_remote_input)
            data_channel.set_closed_handler(on_closed)

            # Start session and wait briefly for readiness
            session.set_data_channel(data_channel)
            await session.execute()
            await asyncio.sleep(0.25)

            # Check if this is a shell session vs a command
            is_shell_session = command.strip().lower() in ['bash', 'zsh', 'sh', 'fish', '/bin/bash', '/bin/zsh', '/bin/sh', '/usr/bin/fish']
            
            if is_shell_session:
                # For shell sessions, replace the parent shell with the requested one
                # so a single 'exit' terminates the session. Do not emit exit codes.
                shell_wrapper = f"exec {command}"
                await data_channel.send_input_data((shell_wrapper + "\n").encode("utf-8"))
                await asyncio.sleep(0.1)
                print("Interactive shell started. Type 'exit' to close the session.")

                # Put terminal into cbreak/no-echo and forward keystrokes using add_reader (non-blocking)
                restore_attrs = None
                stdin_fd = None
                reader_installed = False
                try:
                    import termios
                    import tty
                    import os as _os
                    loop_inner = asyncio.get_running_loop()
                    if sys.stdin.isatty():
                        stdin_fd = sys.stdin.fileno()
                        restore_attrs = termios.tcgetattr(stdin_fd)
                        tty.setcbreak(stdin_fd)  # no-echo, immediate key delivery

                        def _on_stdin_ready() -> None:
                            try:
                                data = _os.read(stdin_fd, 1024)
                                if data:
                                    # schedule async send
                                    asyncio.create_task(data_channel.send_input_data(data))
                            except BlockingIOError:
                                pass
                            except Exception:
                                # Best-effort; ignore stdin read errors
                                pass

                        # Install non-blocking reader; loop.add_reader exists on POSIX
                        if hasattr(loop_inner, "add_reader") and stdin_fd is not None:
                            loop_inner.add_reader(stdin_fd, _on_stdin_ready)
                            reader_installed = True
                except Exception:
                    restore_attrs = None
                    stdin_fd = None
                    reader_installed = False

                try:
                    await closed_async.wait()
                finally:
                    # Remove reader and restore terminal
                    try:
                        if reader_installed and stdin_fd is not None:
                            loop = asyncio.get_running_loop()
                            if hasattr(loop, "remove_reader"):
                                loop.remove_reader(stdin_fd)
                    except Exception:
                        pass
                    try:
                        if restore_attrs is not None and stdin_fd is not None:
                            import termios
                            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, restore_attrs)
                    except Exception:
                        pass
                # Interactive shell: return immediately; no exit code parsing
                return 0
            else:
                # For regular commands, execute with exit code capture and auto-exit
                command_with_exit_capture = f"{command}; echo \"EXIT_CODE:$?\" >&2"
                await data_channel.send_input_data((command_with_exit_capture + "\n").encode("utf-8"))
                await asyncio.sleep(0.05)
                await data_channel.send_input_data(b"exit\n")

            # Wait for session close or timeout
            try:
                await asyncio.wait_for(closed_async.wait(), timeout=timeout_seconds)
                
                # Try to extract exit code from output
                full_output = b''.join(output_buffer).decode('utf-8', errors='ignore')
                if "EXIT_CODE:" in full_output:
                    try:
                        # Find the exit code in stderr output
                        exit_code_line = [line for line in full_output.split('\n') if 'EXIT_CODE:' in line][-1]
                        exit_code = int(exit_code_line.split('EXIT_CODE:')[1].strip())
                    except (ValueError, IndexError):
                        pass
                        
            except asyncio.TimeoutError:
                try:
                    if data_channel.is_open:
                        await data_channel.close()
                except Exception:
                    pass
                exit_code = 124  # Timeout exit code
                
            return exit_code

        return asyncio.run(_run_and_wait())

    def _wait_for_ssm_agent(self, instance_id: str, timeout: int = 300) -> None:
        """Wait until the SSM agent reports the instance as available."""
        ssm = boto3.client("ssm")
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                resp = ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )
                if resp.get("InstanceInformationList"):
                    return
            except Exception:
                pass
            time.sleep(5)
        raise TimeoutError("SSM agent not ready within timeout")


@depends_on(VPC)
class S3Bucket(AWSResource):
    resource_type = "storage"

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        region: str = "us-east-1",
        name: Optional[str] = None,
        **kwargs,
    ):
        self.bucket_name = bucket_name
        self.region = region
        super().__init__(name=name, **kwargs)

    def _create(self) -> str:
        s3 = boto3.client("s3", region_name=self.region)

        if not self.bucket_name:
            import uuid

            self.bucket_name = f"mybucket-{uuid.uuid4().hex[:8]}"

        if self.region != "us-east-1":
            s3.create_bucket(
                Bucket=self.bucket_name,
                CreateBucketConfiguration={"LocationConstraint": self.region},
            )
        else:
            s3.create_bucket(Bucket=self.bucket_name)

        # Update config
        self._config.update({"bucket_name": self.bucket_name, "region": self.region})

        return self.bucket_name

    def _destroy(self):
        if not self._config.get("bucket_name"):
            return

        s3 = boto3.client("s3", region_name=self._config["region"])

        # Delete all objects first
        try:
            response = s3.list_objects_v2(Bucket=self._config["bucket_name"])
            if "Contents" in response:
                objects = [{"Key": obj["Key"]} for obj in response["Contents"]]
                s3.delete_objects(
                    Bucket=self._config["bucket_name"], Delete={"Objects": objects}
                )
        except ClientError:
            pass

        # Delete bucket
        s3.delete_bucket(Bucket=self._config["bucket_name"])

    def _exists(self) -> bool:
        if not self._config.get("bucket_name"):
            return False

        s3 = boto3.client("s3", region_name=self._config["region"])
        try:
            s3.head_bucket(Bucket=self._config["bucket_name"])
            return True
        except ClientError:
            return False


@click.command()
@click.option("--env", default="default", help="Environment to use (default: default)")
@click.option(
    "--destroy", is_flag=True, help="Destroy resources instead of creating them"
)
def main(env, destroy):
    """Main CLI entry point."""
    set_environment(env)
    print(f"Using environment: {env}")

    # Create resources with dependencies
    vpc = VPC(name="cloudrun-preview-vpc", retain=True)
    ec2 = EC2Instance("t3.micro", vpc=vpc, name="web-server")
    s3 = S3Bucket(bucket_name="my-test-bucket", name="data-bucket")

    if destroy:
        # Destroy resources in reverse order
        print("Destroying resources...")
        s3.destroy()
        ec2.destroy()
        vpc.destroy()
    else:
        # Deploy resources
        print("Creating resources...")
        vpc.get_or_create()
        ec2.get_or_create()
        s3.get_or_create()

        print(f"VPC ID: {vpc._config.get('resource_id')}")
        print(f"EC2 Instance ID: {ec2._config.get('resource_id')}")
        print(f"S3 Bucket: {s3._config.get('resource_id')}")


if __name__ == "__main__":
    # Example usage:
    set_environment("production")
    vpc = VPC(retain=True)
    vpc.get_or_create()
    print(f"VPC ID: {vpc._config.get('resource_id')}")
