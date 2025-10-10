import asyncio
import time
from pathlib import Path

import click

from runbox.providers.aws import VPC, EC2Instance
from runbox.providers.digitalocean import Droplet, SSHKey

@click.command()
@click.option("--provider", type=click.Choice(["aws", "digitalocean"], case_sensitive=False), default="aws", help="Cloud provider to use (default: aws)")
@click.option("--instance-type", help="Type of instance to use (AWS: instance type, DO: size slug).", required=True)
@click.option(
    "-s", "--silent", is_flag=True, help="Silent mode - only show instance output"
)
@click.argument("executable", type=click.Path(exists=True))
def run(provider, instance_type, executable, silent):
    """Run an executable on a cloud instance."""
    executable_path = Path(executable)
    executable_name = executable_path.name
    remote_path = f"/tmp/{executable_name}"

    # Normalize provider name
    provider = provider.lower()

    async def run_on_instance():
        start_time = time.time()

        if not silent:
            click.echo(f"🚀 Cloud Run - Executing {executable_name} on {instance_type}")
            click.echo()
            click.echo("📦 Infrastructure Setup")

        if provider == "aws":
            # Create VPC and EC2 instance with retain=False for automatic cleanup
            vpc = VPC(retain=True)
            vpc.get_or_create()

            # Show VPC status (unless silent)
            if not silent:
                if vpc.was_existing:
                    vpc_config = vpc._config
                    cidr = vpc_config.get('cidr_block', 'unknown')
                    click.echo(f"  ✓ VPC: Using existing {vpc.resource_id} ({cidr})")
                else:
                    click.echo(f"  ✓ VPC: Created {vpc.resource_id}")

            # Time EC2 creation
            instance_start = time.time()
            instance = EC2Instance(instance_type=instance_type, vpc=vpc, retain=False)
            instance.get_or_create()
            instance_time = time.time() - instance_start

            # Handle EC2 creation/usage messaging (unless silent)
            if not silent:
                if instance.was_existing:
                    click.echo(f"  ✓ EC2: Using existing {instance.resource_id} ({instance_type})")
                elif instance.was_created:
                    click.echo(f"  ✓ EC2: Created {instance.resource_id} ({instance_type}) [{instance_time:.1f}s]")
                click.echo()
                click.echo("🔧 Instance Preparation")

            # Wait for instance to be ready with timing
            if not silent:
                click.echo("  ⏳ SSM Agent: Waiting for connection...", nl=False)

            ready_start = time.time()
            await instance.wait_for_ssm()
            ready_time = time.time() - ready_start

            if not silent:
                # Clear the line completely and rewrite
                click.echo(f"\r  ✓ SSM Agent: Ready [{ready_time:.1f}s]" + " " * 20)

        elif provider == "digitalocean":
            # Create SSH key and Droplet
            ssh_key = SSHKey(retain=True)
            ssh_key.get_or_create()

            # Show SSH key status (unless silent)
            if not silent:
                if ssh_key.was_existing:
                    click.echo(f"  ✓ SSH Key: Using existing {ssh_key._config.get('key_name')}")
                else:
                    click.echo(f"  ✓ SSH Key: Created {ssh_key._config.get('key_name')}")

            # Time Droplet creation
            instance_start = time.time()
            instance = Droplet(size=instance_type, sshkey=ssh_key, retain=False)
            instance.get_or_create()
            instance_time = time.time() - instance_start

            # Handle Droplet creation/usage messaging (unless silent)
            if not silent:
                if instance.was_existing:
                    click.echo(f"  ✓ Droplet: Using existing {instance.display_name} ({instance_type})")
                elif instance.was_created:
                    click.echo(f"  ✓ Droplet: Created {instance.display_name} ({instance_type}) [{instance_time:.1f}s]")
                click.echo()
                click.echo("🔧 Instance Preparation")

            # Wait for droplet to be ready with timing
            if not silent:
                click.echo("  ⏳ Droplet: Waiting for active state...", nl=False)

            ready_start = time.time()
            await instance.wait_for_active()
            ready_time = time.time() - ready_start

            if not silent:
                # Clear the line completely and rewrite
                click.echo(f"\r  ✓ Droplet: Ready [{ready_time:.1f}s]" + " " * 20)

        else:
            raise ValueError(f"Unknown provider: {provider}")

        try:
            # Time file transfer
            copy_start = time.time()
            if not silent:
                click.echo(f"  ⏳ File Transfer: {executable_name} → {remote_path}...", nl=False)

            # Copy the executable to the instance
            copy_success = await instance.copy_file(str(executable_path), remote_path)
            copy_time = time.time() - copy_start

            if not copy_success:
                if not silent:
                    click.echo("\r  ❌ File Transfer: Failed")
                click.echo("Failed to copy file to instance", err=True)
                return 1

            if not silent:
                click.echo(f"\r  ✓ File Transfer: {executable_name} → {remote_path} [{copy_time:.1f}s]" + " " * 10)

            # Make the file executable (silently)
            chmod_result = instance.run_command(f"chmod +x {remote_path}")
            if chmod_result["status"] != "Success":
                click.echo("Failed to make file executable", err=True)
                return 1

            # Run the executable and show output with clean delimiters
            if not silent:
                click.echo()
                click.echo("▶️  Execution Results")

            exit_code = await instance.run(remote_path)

            return exit_code

        finally:
            # Clean up - instances will be destroyed automatically due to retain=False
            cleanup_start = time.time()
            if not silent and instance.resource_id:
                click.echo()
                click.echo("🧹 Cleanup")
                instance_label = "EC2" if provider == "aws" else "Droplet"
                display_id = instance.resource_id if provider == "aws" else instance.display_name
                click.echo(f"  ⏳ {instance_label}: Terminating {display_id}...", nl=False)

            instance.destroy()
            cleanup_time = time.time() - cleanup_start
            total_time = time.time() - start_time

            if not silent and instance.resource_id:
                instance_label = "EC2" if provider == "aws" else "Droplet"
                display_id = instance.resource_id if provider == "aws" else instance.display_name
                click.echo(f"\r  ✓ {instance_label}: Terminated {display_id} [{cleanup_time:.1f}s]" + " " * 15)
                click.echo()
                click.echo(f"✅ Total execution time: {total_time:.1f}s")

    try:
        exit_code = asyncio.run(run_on_instance())
        if exit_code != 0:
            click.echo(f"Executable exited with code: {exit_code}", err=True)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
