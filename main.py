import asyncio
from pathlib import Path

import click

from commands.auth import auth
from providers.aws import VPC, EC2Instance


@click.group()
def cli():
    """A simple CLI with subcommands."""
    pass


cli.add_command(auth)


@cli.command()
@click.option("--instance-type", help="Type of instance to use.", required=True)
@click.option(
    "--provider", "provider_name", help="Cloud provider to use.", default="aws"
)
@click.argument("executable", type=click.Path(exists=True))
def run(instance_type, provider_name, executable):
    """Run an executable on a cloud instance."""
    if provider_name != "aws":
        click.echo(f"Unsupported provider: {provider_name}")
        return

    executable_path = Path(executable)
    executable_name = executable_path.name
    remote_path = f"/tmp/{executable_name}"

    async def run_on_instance():
        # Create VPC and EC2 instance with retain=False for automatic cleanup
        vpc = VPC(retain=True)
        vpc.get_or_create()

        ec2 = EC2Instance(instance_type=instance_type, vpc=vpc, retain=False)
        ec2.get_or_create()

        try:
            click.echo(f"Copying {executable_name} to instance...")
            
            # Copy the executable to the instance
            copy_success = await ec2.copy_file(str(executable_path), remote_path)
            
            if not copy_success:
                click.echo("Failed to copy file to instance", err=True)
                return 1

            # Make the file executable
            click.echo(f"Making {executable_name} executable...")
            chmod_result = ec2.run_command(f"chmod +x {remote_path}")
            if chmod_result["status"] != "Success":
                click.echo("Failed to make file executable", err=True)
                return 1

            # Run the executable and show output
            click.echo(f"Executing {executable_name} on {instance_type}...")
            exit_code = ec2.run(remote_path)
            
            return exit_code

        finally:
            # Clean up - instances will be destroyed automatically due to retain=False
            click.echo("Cleaning up...")
            ec2.destroy()

    try:
        exit_code = asyncio.run(run_on_instance())
        if exit_code != 0:
            click.echo(f"Executable exited with code: {exit_code}", err=True)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


if __name__ == "__main__":
    cli()
