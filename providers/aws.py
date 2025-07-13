from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Optional, Union

import boto3
import botocore.session
import click
from botocore import xform_name

from providers import Provider


class AWS(Provider):
    def __init__(self):
        self._session = botocore.session.Session()

    def is_valid_service(self, service):
        return service in self._session.get_available_services()

    def is_valid_service_command(self, service, command):
        if not self.is_valid_service(service):
            return False
        # client = boto3.client(service)
        operations: list[str] = self._session.get_service_model(service).operation_names # type: ignore
        return command in [xform_name(cmd) for cmd in operations]

    def can_i(self, service, command):
        click.echo(f"Checking if you can {command} on {service}...")
        iam = boto3.client("iam")
        sts = boto3.client("sts")

        # Get the arn represented by the currently configured credentials
        arn = sts.get_caller_identity()["Arn"]

        action_name = f"{service}:{command}"

        # ResourceArns=[bucket_objects_arn],
        results = iam.simulate_principal_policy(
            PolicySourceArn=arn, ActionNames=["iam:CreateUserBIBBBITYBOB"]
        )

        print(results)
        for result in results["EvaluationResults"]:
            print("%s - %s" % (result["EvalActionName"], result["EvalDecision"]))

    def _verify_permissions(self):
        click.echo("Verifying AWS permissions...")
        return True

    def _create_cloud_run_dependencies(self):
        click.echo("Creating AWS cloud run dependencies...")
        # Create a VPC
        # Create a security group
        # Create an IAM role
        return True

    def create_infra(self):
        self._verify_permissions()
        self._create_cloud_run_dependencies()
        click.echo("Creating AWS infrastructure...")

    def _ami_id_for_instance_type(self, instance_type):
        ssm_parameter = "al2023-ami-minimal-kernel-default-x86_64"
        if re.search(r"\dg\w+\.", instance_type):
            ssm_parameter = "al2023-ami-minimal-kernel-default-arm64"

        return f"resolve:ssm:/aws/service/ami-amazon-linux-latest/{ssm_parameter}"

    def _create_instance(self, instance_type):
        click.echo(f"Creating AWS instance of type {instance_type}...")
        ec2 = boto3.client("ec2")
        response = ec2.run_instances(
            InstanceType=instance_type,
            ImageId=self._ami_id_for_instance_type(instance_type),
            MinCount=1,
            MaxCount=1,
        )
        instance_id = response["Instances"][0]["InstanceId"]
        click.echo(f"Created instance with ID: {instance_id}")

    def run(self, executable, instance_type, options=None):
        self._create_instance(instance_type)
        click.echo(f"Running {executable} on AWS with instance type {instance_type}")


# --- Resource Dependency Decorator and Example Resources ---


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
    _dependents: list['Resource'] = []

    def __init__(self, **kwargs):
        required = getattr(self.__class__, 'depends_on', [])
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
    
    def get_or_create(self):
        """Get or create the resource instance."""
        # ensure dependents are created
        for dep in self._dependents:
            dep.get_or_create()
        # call create on this resource
        self._create()

    @abstractmethod
    def _create(self):
        """Create the resource instance."""
        pass

    


@dataclass
class VPCConfig:
    vpc_id: str
    # Maybe we just store the vpc id and query the rest when needed?
    # security_group_id: str
    # subnet_ids: list[str]
    # internet_gateway_id: str
    # vpc_endpoints: list[str]

class VPC(Resource):
    def __init__(self):
        super().__init__()
        
        self._config: VPCConfig

class AWSEC2InstanceType:
    """
    A simple class to represent an AWS EC2 instance type.
    Allows for validation and ability to provide a short name (like `m5.24xl`)
    """

    def __init__(self, type_name: str):
        # TODO: Add validation for instance type names
        self.type_name = type_name
    
    def __str__(self):
        if self.type_name.endswith("xl"):
            return f"{self.type_name}arge"
        return self.type_name

@depends_on(VPC)
class EC2Instance(Resource):
    vpc: Optional[VPC] = None

    def __init__(self, instance_type: Union[AWSEC2InstanceType,str], vpc: Optional[VPC] = None):
        if isinstance(instance_type, str):
            instance_type = AWSEC2InstanceType(instance_type)
        self.instance_type = instance_type
        super().__init__(vpc=vpc)
    
    def _create(self):
        ec2 = boto3.client("ec2")
        response = ec2.run_instances(
            InstanceType=instance_type,
            ImageId=self._ami_id_for_instance_type(instance_type),
            MinCount=1,
            MaxCount=1,
            security_group_ids=[self.vpc.security_group_id] if self.vpc else None,
            subnet_id=self.vpc.subnet_ids[0] if self.vpc and self.vpc.subnet_ids else None,
        )
        instance_id = response["Instances"][0]["InstanceId"]
    
    # def get_or_create(self):
    #     # click.echo(f"Getting or creating EC2 instance of type {self.instance_type} in VPC {self.vpc}")
    #     # Here we would implement the logic to get or create the EC2 instance
    #     # For now, just return a placeholder
    #     return f"EC2Instance({self.instance_type}, VPC={self.vpc})"


@depends_on(VPC)
class S3Bucket(Resource):
    pass
