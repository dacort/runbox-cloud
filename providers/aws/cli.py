import click

from .compute import EC2Instance
from .network import VPC
from .state import set_environment
from .storage import S3Bucket


@click.command()
@click.option("--env", default="default", help="Environment to use (default: default)")
@click.option(
    "--destroy", is_flag=True, help="Destroy resources instead of creating them"
)
def main(env, destroy):
    """Main CLI entry point."""
    set_environment(env)
    print(f"Using environment: {env}")

    # Create resources with dependencies
    vpc = VPC(name="cloudrun-preview-vpc", retain=True)
    ec2 = EC2Instance("t3.micro", vpc=vpc, name="web-server")
    s3 = S3Bucket(bucket_name="my-test-bucket", name="data-bucket")

    if destroy:
        # Destroy resources in reverse order
        print("Destroying resources...")
        s3.destroy()
        ec2.destroy()
        vpc.destroy()
    else:
        # Deploy resources
        print("Creating resources...")
        vpc.get_or_create()
        ec2.get_or_create()
        s3.get_or_create()

        print(f"VPC ID: {vpc._config.get('resource_id')}")
        print(f"EC2 Instance ID: {ec2._config.get('resource_id')}")
        print(f"S3 Bucket: {s3._config.get('resource_id')}")


if __name__ == "__main__":
    # Example usage:
    set_environment("production")
    vpc = VPC(retain=True)
    vpc.get_or_create()
    print(f"VPC ID: {vpc._config.get('resource_id')}")
