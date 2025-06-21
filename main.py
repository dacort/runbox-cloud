import click

from commands.auth import auth
from providers import Provider
from providers.aws import AWS


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
    provider: Provider
    if provider_name == "aws":
        from providers.aws import AWS

        provider = AWS()
    else:
        click.echo(f"Unsupported provider: {provider_name}")
        return
    click.echo(f"Executing {executable} on {instance_type}")
    provider.run(executable, instance_type)


if __name__ == "__main__":
    cli()
