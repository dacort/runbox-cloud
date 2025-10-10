from pydantic import BaseModel
from typing import Optional, Dict
import yaml

# --- Provider Models ---
class AWSVpcConfig(BaseModel):
    vpc_id: str

class AWSConfig(BaseModel):
    region: Optional[str] = None
    default_instance_type: Optional[str] = None
    vpc: Optional[AWSVpcConfig] = None

# --- Environment Model ---
class EnvironmentConfig(BaseModel):
    aws: Optional[AWSConfig] = None
    # Add more providers here

# --- Top-level Config ---
class FullConfig(BaseModel):
    environments: Dict[str, EnvironmentConfig]

    @classmethod
    def load_yaml(cls, path: str) -> "FullConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls.model_validate(raw)

    def save_yaml(self, path: str) -> None:
        with open(path, "w") as f:
            yaml.dump(self.model_dump(), f, sort_keys=False)


@dataclass
class VPCConfig(ResourceConfig):
    """Configuration for VPC resources."""
    vpc_id: Optional[str] = None
    security_group_id: Optional[str] = None
    subnet_ids: List[str] = field(default_factory=list)
    internet_gateway_id: Optional[str] = None
    cidr_block: Optional[str] = None


@dataclass
class EC2Config(ResourceConfig):
    """Configuration for EC2 instances."""
    instance_id: Optional[str] = None
    instance_type: Optional[str] = None
    ami_id: Optional[str] = None
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None


@dataclass
class S3Config(ResourceConfig):
    """Configuration for S3 buckets."""
    bucket_name: Optional[str] = None
    region: Optional[str] = None

