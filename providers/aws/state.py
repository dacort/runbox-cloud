import yaml
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


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