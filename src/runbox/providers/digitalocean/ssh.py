import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from pydo import Client

from .base import DigitalOceanResource

logger = logging.getLogger(__name__)


def compute_ssh_fingerprint(public_key: str) -> str:
    """Compute SSH key fingerprint (MD5) from public key string.

    DigitalOcean uses MD5 fingerprint format: xx:xx:xx:...:xx
    """
    # Parse the public key (format: "ssh-rsa AAAAB3... comment")
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid public key format")

    key_data = parts[1]  # The base64-encoded key data

    # Decode and compute MD5 hash
    decoded = base64.b64decode(key_data)
    md5_hash = hashlib.md5(decoded).hexdigest()

    # Format as colon-separated pairs
    fingerprint = ":".join(md5_hash[i:i+2] for i in range(0, len(md5_hash), 2))
    return fingerprint


class SSHKey(DigitalOceanResource):
    """Manages SSH keys for DigitalOcean droplets.

    Can either use an existing SSH key or create a new one.
    By default, looks for ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.
    """

    resource_type = "ssh"
    retain_by_default = True

    def __init__(self, public_key_path: Optional[str] = None, key_name: Optional[str] = None, **kwargs):
        """Initialize SSH key resource.

        Args:
            public_key_path: Path to existing public key file (default: auto-detect)
            key_name: Name for the SSH key in DigitalOcean (default: cloudrun-key)
        """
        if public_key_path:
            self.public_key_path = public_key_path
        else:
            # Try to find a public key (prefer ed25519, fallback to rsa)
            self.public_key_path = self._find_default_public_key()
        self.key_name = key_name or "cloudrun-key"
        super().__init__(**kwargs)

    def _find_default_public_key(self) -> str:
        """Find default SSH public key, preferring ed25519 over rsa."""
        candidates = [
            os.path.expanduser("~/.ssh/id_ed25519.pub"),
            os.path.expanduser("~/.ssh/id_rsa.pub"),
        ]

        for path in candidates:
            if Path(path).exists():
                return path

        # If none found, default to id_ed25519.pub (will fail later with helpful message)
        return os.path.expanduser("~/.ssh/id_ed25519.pub")

    def _get_client(self) -> Client:
        """Get DigitalOcean API client."""
        token = os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
        if not token:
            raise ValueError("DIGITALOCEAN_ACCESS_TOKEN environment variable not set")
        return Client(token=token)

    def _create(self) -> str:
        """Create or register SSH key with DigitalOcean."""
        client = self._get_client()

        # Read public key
        key_path = Path(self.public_key_path)
        if not key_path.exists():
            raise FileNotFoundError(f"Public key not found at {self.public_key_path}")

        with open(key_path, "r") as f:
            public_key = f.read().strip()

        # Compute fingerprint of local key
        local_fingerprint = compute_ssh_fingerprint(public_key)

        # Check if key already exists by fingerprint first, then by name
        existing_keys = client.ssh_keys.list()
        for key in existing_keys.get("ssh_keys", []):
            # Match by fingerprint (most reliable)
            if key["fingerprint"] == local_fingerprint:
                logger.info(f"SSH key with fingerprint {local_fingerprint} already exists, using existing key (id: {key['id']}, name: {key['name']})")
                self._config.update({
                    "key_id": str(key["id"]),
                    "fingerprint": key["fingerprint"],
                    "key_name": key["name"],  # Use the existing name
                })
                return str(key["id"])
            # Match by name as fallback
            elif key["name"] == self.key_name:
                logger.info(f"SSH key '{self.key_name}' already exists, using existing key")
                self._config.update({
                    "key_id": str(key["id"]),
                    "fingerprint": key["fingerprint"],
                    "key_name": self.key_name,
                })
                return str(key["id"])

        # Create new key
        req = {
            "name": self.key_name,
            "public_key": public_key,
        }

        response = client.ssh_keys.create(body=req)
        ssh_key = response["ssh_key"]

        # Update config
        self._config.update({
            "key_id": str(ssh_key["id"]),
            "fingerprint": ssh_key["fingerprint"],
            "key_name": self.key_name,
        })

        return str(ssh_key["id"])

    def _destroy(self):
        """Remove SSH key from DigitalOcean."""
        if not self._config.get("key_id"):
            return

        client = self._get_client()

        try:
            client.ssh_keys.delete(ssh_key_identifier=self._config["key_id"])
        except Exception as e:
            logger.warning(f"Failed to delete SSH key: {e}")

    def _exists(self) -> bool:
        """Check if SSH key exists in DigitalOcean."""
        if not self._config.get("key_id"):
            return False

        client = self._get_client()

        try:
            client.ssh_keys.get(ssh_key_identifier=self._config["key_id"])
            return True
        except Exception:
            return False
