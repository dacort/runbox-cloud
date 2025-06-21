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