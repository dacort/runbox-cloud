import asyncio
import logging
import re
import sys
import time
from typing import Optional, Union

import boto3
from botocore.exceptions import ClientError

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
        if re.search(r"\\dg\\w+\\.", self.instance_type.type_name):
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
                sigint_installed = False
                try:
                    import termios
                    import tty
                    import os as _os
                    import signal
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

                        # Forward Ctrl-C (SIGINT) to remote as ETX (0x03)
                        def _on_sigint() -> None:
                            try:
                                asyncio.create_task(data_channel.send_input_data(b"\x03"))
                            except Exception:
                                pass

                        if hasattr(loop_inner, "add_signal_handler"):
                            try:
                                loop_inner.add_signal_handler(signal.SIGINT, _on_sigint)
                                sigint_installed = True
                            except NotImplementedError:
                                sigint_installed = False
                except Exception:
                    restore_attrs = None
                    stdin_fd = None
                    reader_installed = False
                    sigint_installed = False

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
                    # Remove SIGINT handler
                    try:
                        if sigint_installed:
                            loop = asyncio.get_running_loop()
                            if hasattr(loop, "remove_signal_handler"):
                                import signal
                                loop.remove_signal_handler(signal.SIGINT)
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
                command_with_exit_capture = f"{command}; echo \\\"EXIT_CODE:$?\\\" >&2"
                await data_channel.send_input_data((command_with_exit_capture + "\\n").encode("utf-8"))
                await asyncio.sleep(0.05)
                await data_channel.send_input_data(b"exit\\n")

            # Wait for session close or timeout
            try:
                await asyncio.wait_for(closed_async.wait(), timeout=timeout_seconds)
                
                # Try to extract exit code from output
                full_output = b''.join(output_buffer).decode('utf-8', errors='ignore')
                if "EXIT_CODE:" in full_output:
                    try:
                        # Find the exit code in stderr output
                        exit_code_line = [line for line in full_output.split('\\n') if 'EXIT_CODE:' in line][-1]
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
