import asyncio
import time
from pathlib import Path

import click

from providers.aws import VPC, EC2Instance

@click.command()
@click.option("--instance-type", help="Type of instance to use.", required=True)
@click.option(
    "-s", "--silent", is_flag=True, help="Silent mode - only show instance output"
)
@click.argument("executable", type=click.Path(exists=True))
def run(instance_type, executable, silent):
    """Run an executable on a cloud instance."""
    executable_path = Path(executable)
    executable_name = executable_path.name
    remote_path = f"/tmp/{executable_name}"

    async def run_on_instance():
        start_time = time.time()

        if not silent:
            click.echo(f"🚀 Cloud Run - Executing {executable_name} on {instance_type}")
            click.echo()
            click.echo("📦 Infrastructure Setup")

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
        ec2_start = time.time()
        ec2 = EC2Instance(instance_type=instance_type, vpc=vpc, retain=False)
        ec2.get_or_create()
        ec2_time = time.time() - ec2_start

        # Handle EC2 creation/usage messaging (unless silent)
        if not silent:
            if ec2.was_existing:
                click.echo(f"  ✓ EC2: Using existing {ec2.resource_id} ({instance_type})")
            elif ec2.was_created:
                click.echo(f"  ✓ EC2: Created {ec2.resource_id} ({instance_type}) [{ec2_time:.1f}s]")
            click.echo()
            click.echo("🔧 Instance Preparation")

        # Wait for instance to be ready with timing
        if not silent:
            click.echo("  ⏳ SSM Agent: Waiting for connection...", nl=False)

        ssm_start = time.time()
        await ec2.wait_for_ssm()
        ssm_time = time.time() - ssm_start

        if not silent:
            # Clear the line completely and rewrite
            click.echo(f"\r  ✓ SSM Agent: Ready [{ssm_time:.1f}s]" + " " * 20)

        try:
            # Time file transfer
            copy_start = time.time()
            if not silent:
                click.echo(f"  ⏳ File Transfer: {executable_name} → {remote_path}...", nl=False)

            # Copy the executable to the instance
            copy_success = await ec2.copy_file(str(executable_path), remote_path)
            copy_time = time.time() - copy_start

            if not copy_success:
                if not silent:
                    click.echo("\r  ❌ File Transfer: Failed")
                click.echo("Failed to copy file to instance", err=True)
                return 1

            if not silent:
                click.echo(f"\r  ✓ File Transfer: {executable_name} → {remote_path} [{copy_time:.1f}s]" + " " * 10)

            # Make the file executable (silently)
            chmod_result = ec2.run_command(f"chmod +x {remote_path}")
            if chmod_result["status"] != "Success":
                click.echo("Failed to make file executable", err=True)
                return 1

            # Run the executable and show output with clean delimiters
            if not silent:
                click.echo()
                click.echo("▶️  Execution Results")

            exit_code = await ec2.run(remote_path)

            return exit_code

        finally:
            # Clean up - instances will be destroyed automatically due to retain=False
            cleanup_start = time.time()
            if not silent and ec2.resource_id:
                click.echo()
                click.echo("🧹 Cleanup")
                click.echo(f"  ⏳ Instance: Terminating {ec2.resource_id}...", nl=False)

            ec2.destroy()
            cleanup_time = time.time() - cleanup_start
            total_time = time.time() - start_time

            if not silent and ec2.resource_id:
                click.echo(f"\r  ✓ Instance: Terminated {ec2.resource_id} [{cleanup_time:.1f}s]" + " " * 15)
                click.echo()
                click.echo(f"✅ Total execution time: {total_time:.1f}s")

    try:
        exit_code = asyncio.run(run_on_instance())
        if exit_code != 0:
            click.echo(f"Executable exited with code: {exit_code}", err=True)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
