import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import boto3

# Type hints
T = TypeVar("T", bound="Resource")


class ResourceState(Enum):
    """Possible states of a resource."""

    PENDING = "pending"
    CREATING = "creating"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"


@dataclass
class ResourceConfig:
    """Base configuration for all resources."""

    resource_id: Optional[str] = None
    created_at: Optional[str] = None
    state: ResourceState = ResourceState.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "resource_id": self.resource_id,
            "created_at": self.created_at,
            "state": self.state.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceConfig":
        """Create from dictionary."""
        return cls(
            resource_id=data.get("resource_id"),
            created_at=data.get("created_at"),
            state=ResourceState(data.get("state", ResourceState.PENDING.value)),
            metadata=data.get("metadata", {}),
        )


class StateManager:
    """Manages resource state persistence."""

    def __init__(self, state_file: str = "cloud_resources.json"):
        self.state_file = Path(state_file)
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _load_state(self):
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self._state = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                self._state = {}

    def save_state(self):
        """Save state to file."""
        with open(self.state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    def get_resource_config(self, resource_key: str) -> Optional[ResourceConfig]:
        """Get configuration for a resource."""
        if resource_key in self._state:
            return ResourceConfig.from_dict(self._state[resource_key])
        return None

    def set_resource_config(self, resource_key: str, config: ResourceConfig):
        """Set configuration for a resource."""
        self._state[resource_key] = config.to_dict()
        self.save_state()

    def remove_resource(self, resource_key: str):
        """Remove a resource from state."""
        if resource_key in self._state:
            del self._state[resource_key]
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

    def __init__(self, retain: Optional[bool] = None, **kwargs):
        self.retain = retain if retain is not None else self.retain_by_default
        self._dependents: List["Resource"] = []
        self._config: Optional[ResourceConfig] = None
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
        return f"{self.provider}:{self.resource_type}:{self.__class__.__name__}"

    def _load_config(self):
        """Load configuration from state manager."""
        if self.retain:
            self._config = self._state_manager.get_resource_config(self.resource_key)

    def _save_config(self):
        """Save configuration to state manager."""
        if self.retain and self._config:
            self._state_manager.set_resource_config(self.resource_key, self._config)

    def get_or_create(self) -> "Resource":
        """Get or create the resource instance."""
        # Check if we have existing config and resource exists
        if self._config and self._config.resource_id:
            if self._exists():
                print(
                    f"Using existing {self.__class__.__name__}: {self._config.resource_id}"
                )
                return self

        # Ensure dependents are created first
        for dep in self._dependents:
            dep.get_or_create()

        # Create this resource
        print(f"Creating {self.__class__.__name__}...")
        self._config = ResourceConfig(
            state=ResourceState.CREATING, created_at=datetime.now().isoformat()
        )

        try:
            resource_id = self._create()
            self._config.resource_id = resource_id
            self._config.state = ResourceState.ACTIVE
            self._save_config()
            print(f"Created {self.__class__.__name__}: {resource_id}")
        except Exception as e:
            self._config.state = ResourceState.ERROR
            self._save_config()
            raise e

        return self

    def destroy(self):
        """Destroy the resource."""
        if not self._config or not self._config.resource_id:
            return

        print(f"Destroying {self.__class__.__name__}: {self._config.resource_id}")
        self._config.state = ResourceState.DELETING
        self._save_config()

        try:
            self._destroy()
            self._config.state = ResourceState.DELETED
            if not self.retain:
                self._state_manager.remove_resource(self.resource_key)
            else:
                self._save_config()
        except Exception as e:
            self._config.state = ResourceState.ERROR
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


# AWS-specific configurations
@dataclass
class VPCConfig(ResourceConfig):
    vpc_id: Optional[str] = None
    security_group_id: Optional[str] = None
    subnet_ids: List[str] = field(default_factory=list)
    internet_gateway_id: Optional[str] = None


@dataclass
class EC2Config(ResourceConfig):
    instance_id: Optional[str] = None
    instance_type: Optional[str] = None
    ami_id: Optional[str] = None
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None


@dataclass
class S3Config(ResourceConfig):
    bucket_name: Optional[str] = None
    region: Optional[str] = None


# AWS Resources
class AWSResource(Resource):
    """Base class for AWS resources."""

    provider = "aws"


class VPC(AWSResource):
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
                        {"Key": "Name", "Value": "cloudrun-preview-vpc"},
                        {"Key": "cloudrun-version", "Value": "v0.0.1"},
                    ],
                }
            ],
        )
        vpc_id = vpc_response["Vpc"]["VpcId"]

        # Create internet gateway
        igw_response = ec2.create_internet_gateway()
        igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        # Create subnet
        subnet_response = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.120.1.0/24")
        subnet_id = subnet_response["Subnet"]["SubnetId"]

        # Create security group
        sg_response = ec2.create_security_group(
            GroupName=f"cloudrun-{vpc_id}",
            Description="Default security group",
            VpcId=vpc_id,
        )
        sg_id = sg_response["GroupId"]

        # Update config
        if not isinstance(self._config, VPCConfig):
            self._config = VPCConfig.from_dict(self._config.to_dict())

        self._config.vpc_id = vpc_id
        self._config.security_group_id = sg_id
        self._config.subnet_ids = [subnet_id]
        self._config.internet_gateway_id = igw_id

        return vpc_id

    def _destroy(self):
        if not isinstance(self._config, VPCConfig):
            return

        ec2 = boto3.client("ec2")

        # Detach and delete internet gateway
        if self._config.internet_gateway_id:
            ec2.detach_internet_gateway(
                InternetGatewayId=self._config.internet_gateway_id,
                VpcId=self._config.vpc_id,
            )
            ec2.delete_internet_gateway(
                InternetGatewayId=self._config.internet_gateway_id
            )

        # Delete subnets
        for subnet_id in self._config.subnet_ids:
            ec2.delete_subnet(SubnetId=subnet_id)

        # Delete security group
        if self._config.security_group_id:
            ec2.delete_security_group(GroupId=self._config.security_group_id)

        # Delete VPC
        ec2.delete_vpc(VpcId=self._config.vpc_id)

    def _exists(self) -> bool:
        if not isinstance(self._config, VPCConfig) or not self._config.vpc_id:
            return False

        ec2 = boto3.client("ec2")
        try:
            response = ec2.describe_vpcs(VpcIds=[self._config.vpc_id])
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


@depends_on(VPC)
class EC2Instance(AWSResource):
    resource_type = "compute"

    def __init__(
        self,
        instance_type: Union[AWSEC2InstanceType, str],
        ami_id: Optional[str] = None,
        vpc: Optional[VPC] = None,
        **kwargs,
    ):
        if isinstance(instance_type, str):
            instance_type = AWSEC2InstanceType(instance_type)
        self.instance_type = instance_type
        self.ami_id = ami_id or self._get_default_ami()
        super().__init__(vpc=vpc, **kwargs)

    def _get_default_ami(self) -> str:
        """Get default AMI for the instance type."""
        # This is a simplified example and only supports al2023 on x86 or arm64
        ssm_parameter = "al2023-ami-minimal-kernel-default-x86_64"
        if re.search(r"\dg\w+\.", self.instance_type.type_name):
            ssm_parameter = "al2023-ami-minimal-kernel-default-arm64"

        return f"resolve:ssm:/aws/service/ami-amazon-linux-latest/{ssm_parameter}"

    def _create(self) -> str:
        ec2 = boto3.client("ec2")

        # Get VPC config
        vpc_config = self.vpc._config
        if not isinstance(vpc_config, VPCConfig):
            raise ValueError("VPC configuration is not available")

        response = ec2.run_instances(
            InstanceType=self.instance_type.type_name,
            ImageId=self.ami_id,
            MinCount=1,
            MaxCount=1,
            SecurityGroupIds=[vpc_config.security_group_id]
            if vpc_config.security_group_id
            else [],
            SubnetId=vpc_config.subnet_ids[0] if vpc_config.subnet_ids else None,
        )

        instance_id = response["Instances"][0]["InstanceId"]

        # Update config
        if not isinstance(self._config, EC2Config):
            self._config = EC2Config.from_dict(self._config.to_dict())

        self._config.instance_id = instance_id
        self._config.instance_type = self.instance_type.type_name
        self._config.ami_id = self.ami_id

        return instance_id

    def _destroy(self):
        if not isinstance(self._config, EC2Config) or not self._config.instance_id:
            return

        ec2 = boto3.client("ec2")
        ec2.terminate_instances(InstanceIds=[self._config.instance_id])

    def _exists(self) -> bool:
        if not isinstance(self._config, EC2Config) or not self._config.instance_id:
            return False

        ec2 = boto3.client("ec2")
        try:
            response = ec2.describe_instances(InstanceIds=[self._config.instance_id])
            instances = []
            for reservation in response["Reservations"]:
                instances.extend(reservation["Instances"])

            return len(instances) > 0 and instances[0]["State"]["Name"] != "terminated"
        except:
            return False


@depends_on(VPC)
class S3Bucket(AWSResource):
    resource_type = "storage"

    def __init__(
        self, bucket_name: Optional[str] = None, region: str = "us-east-1", **kwargs
    ):
        self.bucket_name = bucket_name
        self.region = region
        super().__init__(**kwargs)

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
        if not isinstance(self._config, S3Config):
            self._config = S3Config.from_dict(self._config.to_dict())

        self._config.bucket_name = self.bucket_name
        self._config.region = self.region

        return self.bucket_name

    def _destroy(self):
        if not isinstance(self._config, S3Config) or not self._config.bucket_name:
            return

        s3 = boto3.client("s3", region_name=self._config.region)

        # Delete all objects first
        try:
            response = s3.list_objects_v2(Bucket=self._config.bucket_name)
            if "Contents" in response:
                objects = [{"Key": obj["Key"]} for obj in response["Contents"]]
                s3.delete_objects(
                    Bucket=self._config.bucket_name, Delete={"Objects": objects}
                )
        except:
            pass

        # Delete bucket
        s3.delete_bucket(Bucket=self._config.bucket_name)

    def _exists(self) -> bool:
        if not isinstance(self._config, S3Config) or not self._config.bucket_name:
            return False

        s3 = boto3.client("s3", region_name=self._config.region)
        try:
            s3.head_bucket(Bucket=self._config.bucket_name)
            return True
        except:
            return False


# Example usage
if __name__ == "__main__":
    # Create resources with dependencies
    vpc = VPC(retain=True)
    ec2 = EC2Instance("t3.micro", vpc=vpc)
    s3 = S3Bucket(bucket_name="my-test-bucket")

    # Deploy resources
    vpc.get_or_create()
    ec2.get_or_create()
    s3.get_or_create()

    # Later, you can destroy them
    # s3.destroy()
    # ec2.destroy()
    # vpc.destroy()  # Only if not retained
