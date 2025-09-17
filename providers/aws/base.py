from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, TypeVar

from providers import Provider

from .state import ResourceState, StateManager

# Type hints
T = TypeVar("T", bound="Resource")


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

    def _save_config(self):
        """Save configuration to state manager."""
        if self.retain and self._config:
            self._state_manager.set_resource_config(self.resource_key, self._config)

    @property
    def resource_id(self) -> str:
        """Get the resource ID."""
        return self._config.get("resource_id", "")

    @property
    def was_created(self) -> bool:
        """True if resource was created in this session (not existing)."""
        return getattr(self, '_was_created', False)

    @property
    def was_existing(self) -> bool:
        """True if resource was existing (not created in this session)."""
        return getattr(self, '_was_existing', False)

    def get_or_create(self) -> "Resource":
        """Get or create the resource instance."""
        # Check if we have existing config and resource exists
        if self._config and self._config.get("resource_id"):
            if self._exists():
                self._was_existing = True
                self._was_created = False
                return self

        # Ensure dependents are created first
        for dep in self._dependents:
            dep.get_or_create()

        # Create this resource
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
            self._was_created = True
            self._was_existing = False
        except Exception as e:
            self._config["state"] = ResourceState.ERROR.value
            self._save_config()
            raise e

        return self

    def destroy(self):
        """Destroy the resource."""
        if not self._config or not self._config.get("resource_id"):
            return

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
        import click

        click.echo(f"Running {executable} on AWS with instance type {instance_type}")


class AWSResource(Resource):
    """Base class for AWS resources."""

    provider = "aws"
