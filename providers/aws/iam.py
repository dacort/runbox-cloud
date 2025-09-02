import json
import logging
from datetime import datetime
from typing import Optional

import boto3

from .base import AWSResource
from .decorators import depends_on

logger = logging.getLogger(__name__)


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