import logging
import re
import time
from pathlib import Path
from typing import Optional, Union

import boto3
from botocore.exceptions import ClientError
from pyssm_client.cli.main import SessionManagerPlugin
from pyssm_client.cli.types import ConnectArguments
from pyssm_client.file_transfer.client import FileTransferClient
from pyssm_client.file_transfer.types import FileTransferOptions

from .base import AWSResource
from .decorators import depends_on
from .iam import EC2InstanceProfile
from .network import VPC

logger = logging.getLogger(__name__)


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
        root_volume_size: int = 100,
        **kwargs,
    ):
        if isinstance(instance_type, str):
            instance_type = AWSEC2InstanceType(instance_type)
        self.instance_type = instance_type
        self.ami_id = ami_id or self._get_default_ami()
        self.root_volume_size = root_volume_size
        super().__init__(vpc=vpc, ec2instanceprofile=ec2instanceprofile, **kwargs)

    def _get_default_ami(self) -> str:
        """Get default AMI for the instance type."""
        # TODO: Move this into the instance type class
        # This is a simplified example and only supports al2023 on x86 or arm64
        ssm_parameter = "al2023-ami-minimal-kernel-default-x86_64"
        if re.search(r"\\dg\\w+\\.", self.instance_type.type_name):
            ssm_parameter = "al2023-ami-minimal-kernel-default-arm64"

        return f"resolve:ssm:/aws/service/ami-amazon-linux-latest/{ssm_parameter}"

    def _get_root_device_name(self, ec2_client, ami_id: str) -> str:
        """Get the root device name from the AMI."""
        # Handle SSM parameter resolution - need to resolve the actual AMI ID first
        if ami_id.startswith("resolve:ssm:"):
            ssm = boto3.client("ssm", region_name=ec2_client.meta.region_name)
            param_name = ami_id.replace("resolve:ssm:", "")
            response = ssm.get_parameter(Name=param_name)
            ami_id = response["Parameter"]["Value"]

        response = ec2_client.describe_images(ImageIds=[ami_id])
        if not response["Images"]:
            # Fallback to common default
            return "/dev/xvda"
        return response["Images"][0].get("RootDeviceName", "/dev/xvda")

    def _create(self) -> str:
        ec2 = boto3.client("ec2")

        # Get VPC config
        vpc_config = self.vpc._config
        if not vpc_config.get("security_group_id"):
            raise ValueError("VPC configuration is not available")

        # Get the correct root device name for this AMI
        root_device_name = self._get_root_device_name(ec2, self.ami_id)

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
            "BlockDeviceMappings": [
                {
                    "DeviceName": root_device_name,
                    "Ebs": {
                        "VolumeSize": self.root_volume_size,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ],
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

    async def wait_for_ssm(self, timeout: int = 300) -> None:
        """Wait until the instance is in 'running' state and SSM agent is ready."""
        if not self._config.get("instance_id"):
            raise ValueError("Instance is not created; call get_or_create() first")

        instance_id = self._config["instance_id"]

        # Wait for SSM agent to be ready
        self._wait_for_ssm_agent(instance_id, timeout)

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
            "shell",  # Generic shell request
            "bash",  # Bash shell
            "/bin/bash",  # Full path bash
            "zsh",  # Zsh shell
            "/bin/zsh",  # Full path zsh
            "sh",  # Generic shell
            "/bin/sh",  # Full path sh
            "fish",  # Fish shell
            "/usr/bin/fish",  # Full path fish
        ]

        # Check if the command is exactly a shell command or starts with one
        for shell in shell_commands:
            if command_lower == shell or command_lower.startswith(shell + " "):
                return True

        # Also check for some other interactive commands that benefit from real-time I/O
        interactive_patterns = [
            "tail -f",  # Log following
            "watch",  # Periodic updates
            "top",  # Interactive monitoring
            "htop",  # Interactive monitoring
        ]

        for pattern in interactive_patterns:
            if pattern in command_lower:
                return True

        return False

    async def run(self, command: str, timeout_seconds: int = 300, mode: str = "auto") -> int:
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
            raise ValueError(
                f"Invalid mode '{mode}'. Use 'auto', 'interactive', or 'batch'"
            )

        if use_interactive:
            # Handle special case of generic "shell" command
            if command.lower().strip() == "shell":
                command = "bash"  # Default to bash for generic shell request
            return await self.run_interactive(command, timeout_seconds)
        else:
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

    async def run_interactive(self, command: str, timeout_seconds: int = 300) -> int:
        """Run an interactive session using the plugin's high-level runner.

        Requires plugin support for initial_input in ConnectArguments.
        """
        if not self._config.get("instance_id"):
            raise ValueError("Instance is not created; call get_or_create() first")

        instance_id = self._config["instance_id"]

        # Start an SSM session to obtain StreamUrl/TokenValue
        ssm = boto3.client("ssm")
        start = ssm.start_session(Target=instance_id)

        # Default to bash for generic 'shell'
        if command.lower().strip() == "shell":
            command = "bash"

        args = ConnectArguments(
            session_id=start["SessionId"],
            stream_url=start["StreamUrl"],
            token_value=start["TokenValue"],
            target=instance_id,
            session_type="Standard_Stream",
            initial_input=f"exec {command}",
        )

        plugin = SessionManagerPlugin()
        return await plugin.run_session(args)

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

    async def copy_file(self, local_path: str, remote_path: str, timeout_seconds: int = 300) -> bool:
        """Copy a file to the instance using the file transfer client.
        
        Args:
            local_path: Path to local file to copy
            remote_path: Destination path on the instance
            timeout_seconds: Maximum time to wait for copy operation
            
        Returns:
            True if copy was successful, False otherwise
        """
        if not self._config.get("instance_id"):
            raise ValueError("Instance is not created; call get_or_create() first")

        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"Local file not found: {local_file}")

        instance_id = self._config["instance_id"]

        # Use the file transfer client directly for better large file handling
        client = FileTransferClient()

        # Create file transfer options (no progress callback to avoid spam)
        options = FileTransferOptions(
            chunk_size=32 * 1024,  # 32KB chunks for good performance
            verify_checksum=True,
        )

        try:
            success = await client.upload_file(
                local_path=str(local_file),
                remote_path=remote_path,
                target=instance_id,
                options=options,
            )
            return success
        except Exception as e:
            print(f"Failed to copy file: {e}")
            return False
