import re

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
        operations = self._session.get_service_model(service).operation_names
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

class Resource:
    ### A resource for a provider, like an S3 bucket or EC2 instance.
    pass

class VPC(Resource):
    pass

class EC2Instance(Resource):
    pass