# AWS Provider Module
# This module provides AWS resource management capabilities

# Main AWS provider class
from .base import AWS, AWSResource, Resource

# CLI
from .cli import main

# Compute resources  
from .compute import AWSEC2InstanceType, EC2Instance

# Decorators
from .decorators import depends_on

# IAM resources
from .iam import EC2IAMrole, EC2InstanceProfile

# Network resources
from .network import VPC

# State management
from .state import ResourceState, StateManager, get_environment, set_environment

# Storage resources
from .storage import S3Bucket

# Expose commonly used classes for backward compatibility
__all__ = [
    # Core
    "AWS",
    "AWSResource", 
    "Resource",
    # State
    "ResourceState",
    "StateManager",
    "set_environment",
    "get_environment",
    # Decorator
    "depends_on",
    # Resources
    "VPC",
    "EC2IAMrole",
    "EC2InstanceProfile", 
    "AWSEC2InstanceType",
    "EC2Instance",
    "S3Bucket",
    # CLI
    "main",
]