import base64
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from pydo import Client

from .base import DigitalOceanResource
from .ssh import SSHKey

logger = logging.getLogger(__name__)


def check_doctl_installed():
    """Check if doctl CLI is installed and available."""
    if shutil.which("doctl") is None:
        raise RuntimeError(
            "doctl CLI is not installed or not in PATH. "
            "Please install it from https://docs.digitalocean.com/reference/doctl/how-to/install/"
        )


def depends_on(*dependencies):
    """Decorator to specify resource dependencies."""
    def decorator(cls):
        cls.depends_on = dependencies
        return cls
    return decorator


@depends_on(SSHKey)
class Droplet(DigitalOceanResource):
    """DigitalOcean Droplet (virtual machine) resource.

    Creates a Droplet with the specified size and image.
    Uses doctl for command execution and file transfer.
    """

    resource_type = "compute"
    sshkey: Optional[SSHKey]

    def __init__(
        self,
        size: str,
        image: str = "ubuntu-24-04-x64",
        region: str = "nyc3",
        sshkey: Optional[SSHKey] = None,
        volume_size: Optional[int] = None,
        **kwargs,
    ):
        """Initialize Droplet resource.

        Args:
            size: Droplet size slug (e.g., 's-1vcpu-1gb', 'c-2', 'm-2vcpu-16gb')
            image: Image slug or ID (default: ubuntu-24-04-x64)
            region: Region slug (default: nyc3)
            sshkey: SSHKey resource instance
            volume_size: Optional block storage volume size in GB to attach.
                        Note: DO root disk is fixed to droplet size, so this
                        creates an additional volume mounted at /mnt/data.
        """
        self.size = size
        self.image = image
        self.region = region
        if volume_size is not None:
            if not isinstance(volume_size, int):
                raise TypeError("volume_size must be an integer number of gigabytes (GB).")
            # DigitalOcean block storage volumes must be between 1 GB and 16 TB (16384 GB).
            if volume_size < 1 or volume_size > 16 * 1024:
                raise ValueError(
                    "volume_size must be between 1 GB and 16384 GB (16 TB), in whole GB increments."
                )
        self.volume_size = volume_size
        super().__init__(sshkey=sshkey, **kwargs)

    def _get_client(self) -> Client:
        """Get DigitalOcean API client."""
        token = os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
        if not token:
            raise ValueError("DIGITALOCEAN_ACCESS_TOKEN environment variable not set")
        return Client(token=token)

    def _create(self) -> str:
        """Create a new Droplet."""
        # Check doctl is installed before attempting to create
        check_doctl_installed()

        client = self._get_client()

        # Build droplet request
        req = {
            "name": f"cloudrun-{int(time.time())}",
            "region": self.region,
            "size": self.size,
            "image": self.image,
            "ssh_keys": [self.sshkey._config["key_id"]] if self.sshkey else [],
            "backups": False,
            "ipv6": False,
            "monitoring": False,
            "tags": ["cloudrun"],
        }

        # Create droplet
        response = client.droplets.create(body=req)
        droplet = response["droplet"]
        droplet_id = str(droplet["id"])
        droplet_name = droplet["name"]

        # Update config
        self._config.update({
            "droplet_id": droplet_id,
            "droplet_name": droplet_name,
            "size": self.size,
            "image": self.image,
            "region": self.region,
        })

        # Create and attach block storage volume if requested
        if self.volume_size:
            volume_name = f"{droplet_name}-data"
            volume_req = {
                "size_gigabytes": self.volume_size,
                "name": volume_name,
                "region": self.region,
                "filesystem_type": "ext4",
                "filesystem_label": "data",
            }
            vol_response = client.volumes.create(body=volume_req)
            volume_id = vol_response["volume"]["id"]
            self._config["volume_id"] = volume_id
            self._config["volume_name"] = volume_name
            logger.info(f"Created volume {volume_name} ({self.volume_size}GB)")

        return droplet_id

    def _destroy(self):
        """Destroy the Droplet and any attached volumes."""
        client = self._get_client()

        # Destroy volume first if it exists
        if self._config.get("volume_id"):
            try:
                client.volumes.delete(volume_id=self._config["volume_id"])
                logger.info(f"Destroyed volume {self._config.get('volume_name')}")
            except Exception as e:
                logger.warning(f"Failed to destroy volume: {e}")

        if not self._config.get("droplet_id"):
            return

        try:
            client.droplets.destroy(droplet_id=self._config["droplet_id"])
        except Exception as e:
            logger.warning(f"Failed to destroy droplet: {e}")

    def _exists(self) -> bool:
        """Check if Droplet exists."""
        if not self._config.get("droplet_id"):
            return False

        client = self._get_client()

        try:
            response = client.droplets.get(droplet_id=self._config["droplet_id"])
            droplet = response.get("droplet", {})
            return droplet.get("status") != "archive"
        except:
            return False

    async def wait_for_active(self, timeout: int = 300) -> None:
        """Wait until the droplet is in 'active' state and SSH is ready."""
        if not self._config.get("droplet_id"):
            raise ValueError("Droplet is not created; call get_or_create() first")

        client = self._get_client()
        droplet_id = self._config["droplet_id"]
        start_time = time.time()

        # First, wait for droplet to be active
        while time.time() - start_time < timeout:
            try:
                response = client.droplets.get(droplet_id=droplet_id)
                droplet = response["droplet"]
                status = droplet.get("status")

                if status == "active":
                    # Store IP address
                    networks = droplet.get("networks", {})
                    v4_networks = networks.get("v4", [])
                    if v4_networks:
                        self._config["ip_address"] = v4_networks[0]["ip_address"]
                        self._save_config()
                    break

                time.sleep(5)
            except Exception as e:
                logger.warning(f"Error checking droplet status: {e}")
                time.sleep(5)
        else:
            raise TimeoutError(f"Droplet not active within {timeout} seconds")

        # Now wait for SSH to be ready
        await self._wait_for_ssh(timeout - int(time.time() - start_time))

        # Attach and mount volume if one was created
        if self._config.get("volume_id"):
            await self._attach_and_mount_volume()

    async def _attach_and_mount_volume(self) -> None:
        """Attach the block storage volume and mount it at /mnt/data."""
        volume_id = self._config.get("volume_id")
        droplet_id = self._config.get("droplet_id")

        if not volume_id or not droplet_id:
            return

        client = self._get_client()

        # Attach volume to droplet
        attach_req = {
            "type": "attach",
            "droplet_id": int(droplet_id),
        }
        client.volume_actions.post(volume_id=volume_id, body=attach_req)
        logger.info(f"Attached volume {self._config.get('volume_name')} to droplet")

        # Wait a moment for the volume to be attached
        time.sleep(5)

        # Mount the volume - DO volumes with filesystem_type are auto-formatted
        # but need to be mounted. The device is /dev/disk/by-id/scsi-0DO_Volume_<name>
        volume_name = self._config.get("volume_name")
        mount_commands = [
            "mkdir -p /mnt/data",
            f"mount -o defaults,nofail,discard,noatime /dev/disk/by-id/scsi-0DO_Volume_{volume_name} /mnt/data",
        ]
        for cmd in mount_commands:
            result = self.run_command(cmd)
            if result["status"] != "Success":
                logger.warning(f"Mount command failed: {result['stderr']}")

        logger.info("Volume mounted at /mnt/data")

    async def _wait_for_ssh(self, timeout: int = 120) -> None:
        """Wait until SSH is accepting connections on the droplet.

        Uses native SSH with auto-accept to handle host key on first connect.
        """
        if not self._config.get("droplet_id"):
            raise ValueError("Droplet is not created")

        ip_address = self._config.get("ip_address")
        if not ip_address:
            raise ValueError("Droplet IP address not available")

        ssh_key_path = self._get_ssh_key_path()
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                # Use native SSH with StrictHostKeyChecking=accept-new (auto-accepts new hosts)
                result = subprocess.run(
                    [
                        "ssh",
                        "-i", ssh_key_path,
                        "-o", "StrictHostKeyChecking=accept-new",
                        "-o", "ConnectTimeout=5",
                        f"root@{ip_address}",
                        "echo ready"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode == 0:
                    logger.info("SSH connection ready")
                    return

                # Connection failed, retry
                logger.debug(f"SSH not ready yet: {result.stderr}")
                time.sleep(5)
            except subprocess.TimeoutExpired:
                logger.debug("SSH connection attempt timed out")
                time.sleep(5)
            except Exception as e:
                logger.debug(f"SSH connection attempt failed: {e}")
                time.sleep(5)

        raise TimeoutError(f"SSH not ready within {timeout} seconds")

    @property
    def display_name(self) -> str:
        """Get a user-friendly display name for the droplet."""
        name = self._config.get("droplet_name", "")
        droplet_id = self._config.get("droplet_id", "")
        if name and droplet_id:
            return f"{name} ({droplet_id})"
        elif name:
            return name
        elif droplet_id:
            return droplet_id
        return "unknown"

    def _get_ssh_key_path(self) -> str:
        """Get the private key path corresponding to the public key."""
        if not self.sshkey:
            raise ValueError("SSH key is required but not configured")

        # Get the public key path and derive private key path
        public_key_path = self.sshkey.public_key_path
        # Remove .pub extension to get private key
        if public_key_path.endswith(".pub"):
            return public_key_path[:-4]
        else:
            raise ValueError(f"Unexpected public key path format: {public_key_path}")


    def run_command(self, command: str, timeout_seconds: int = 300) -> dict:
        """Run a shell command on the droplet via doctl.

        Returns a dictionary with:
        - status: 'Success' or 'Failed'
        - stdout: command output
        - stderr: command errors
        - exit_code: command exit code
        """
        if not self._config.get("droplet_id"):
            raise ValueError("Droplet is not created; call get_or_create() first")

        droplet_id = self._config["droplet_id"]
        ssh_key_path = self._get_ssh_key_path()

        try:
            result = subprocess.run(
                ["doctl", "compute", "ssh", droplet_id, "--ssh-key-path", ssh_key_path, "--ssh-command", command],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )

            return {
                "status": "Success" if result.returncode == 0 else "Failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "TimedOut",
                "stdout": "",
                "stderr": "Command timed out",
                "exit_code": -1,
            }
        except Exception as e:
            return {
                "status": "Failed",
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
            }

    async def run(self, command: str, timeout_seconds: int = 300, mode: str = "auto") -> int:
        """Run a shell command on the droplet with intelligent mode selection.

        Args:
            command: Shell command to execute
            timeout_seconds: Maximum execution time
            mode: Execution mode - 'auto', 'interactive', 'batch'
                - 'auto': Choose based on command characteristics
                - 'interactive': Use doctl ssh for real-time I/O
                - 'batch': Use doctl ssh with --ssh-command for simple execution

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
            return await self.run_interactive(command, timeout_seconds)
        else:
            result = self.run_command(command, timeout_seconds)

            # Print output for batch mode
            if result["stdout"]:
                print(result["stdout"], end="")
            if result["stderr"]:
                import sys
                print(result["stderr"], file=sys.stderr, end="")

            # Return exit code
            exit_code = result["exit_code"]
            return int(exit_code) if exit_code != -1 else 1

    def _should_use_interactive_mode(self, command: str) -> bool:
        """Determine if command should use interactive mode based on characteristics."""
        command_lower = command.lower().strip()

        # Shell invocations - these should always be interactive
        shell_commands = [
            "shell", "bash", "/bin/bash", "zsh", "/bin/zsh",
            "sh", "/bin/sh", "fish", "/usr/bin/fish",
        ]

        # Check if the command is exactly a shell command or starts with one
        for shell in shell_commands:
            if command_lower == shell or command_lower.startswith(shell + " "):
                return True

        # Also check for some other interactive commands
        interactive_patterns = ["tail -f", "watch", "top", "htop"]
        for pattern in interactive_patterns:
            if pattern in command_lower:
                return True

        return False

    async def run_interactive(self, command: str, timeout_seconds: int = 300) -> int:
        """Run an interactive session using doctl.

        Args:
            command: Command to execute interactively
            timeout_seconds: Maximum execution time

        Returns:
            Exit code of the command
        """
        if not self._config.get("droplet_id"):
            raise ValueError("Droplet is not created; call get_or_create() first")

        droplet_id = self._config["droplet_id"]
        ssh_key_path = self._get_ssh_key_path()

        # For generic 'shell', just use doctl ssh without command
        if command.lower().strip() == "shell":
            result = subprocess.run(
                ["doctl", "compute", "ssh", droplet_id, "--ssh-key-path", ssh_key_path],
                timeout=timeout_seconds,
            )
            return result.returncode

        # For specific commands, execute them interactively
        result = subprocess.run(
            ["doctl", "compute", "ssh", droplet_id, "--ssh-key-path", ssh_key_path, "--ssh-command", command],
            timeout=timeout_seconds,
        )
        return result.returncode

    async def copy_file(self, local_path: str, remote_path: str, timeout_seconds: int = 300) -> bool:
        """Copy a file to the droplet using base64 encoding via doctl.

        Args:
            local_path: Path to local file to copy
            remote_path: Destination path on the droplet
            timeout_seconds: Maximum time to wait for copy operation

        Returns:
            True if copy was successful, False otherwise
        """
        if not self._config.get("droplet_id"):
            raise ValueError("Droplet is not created; call get_or_create() first")

        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"Local file not found: {local_file}")

        droplet_id = self._config["droplet_id"]
        ssh_key_path = self._get_ssh_key_path()

        try:
            # Read and base64 encode the file
            with open(local_file, "rb") as f:
                file_data = f.read()
            encoded = base64.b64encode(file_data).decode('ascii')

            # Transfer via doctl ssh with base64 decode
            decode_command = f"base64 -d > {remote_path}"
            result = subprocess.run(
                ["doctl", "compute", "ssh", droplet_id, "--ssh-key-path", ssh_key_path, "--ssh-command", decode_command],
                input=encoded,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )

            if result.returncode == 0:
                return True
            else:
                logger.error(f"Failed to copy file: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to copy file: {e}")
            return False
