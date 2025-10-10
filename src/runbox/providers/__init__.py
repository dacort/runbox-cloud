from abc import ABC, abstractmethod
from typing import Optional


class ProviderOptions(ABC):
    """Options for the provider."""
    pass

class Provider(ABC):

    @abstractmethod
    def run(self, executable: str, instance_type: str, options: Optional[ProviderOptions] = None):
        """Run the executable on the provider."""
        pass


# Provider registry
PROVIDERS = {
    "aws": "providers.aws.base.AWS",
    "digitalocean": "providers.digitalocean.base.DigitalOcean",
}


def get_provider(name: str) -> Provider:
    """Get a provider instance by name."""
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {name}. Available: {', '.join(PROVIDERS.keys())}")

    module_path, class_name = PROVIDERS[name].rsplit(".", 1)
    module = __import__(module_path, fromlist=[class_name])
    provider_class = getattr(module, class_name)
    return provider_class()