import click

from runbox.commands.run import run
from runbox.commands.shell import shell


@click.group()
def cli():
    """A simple CLI with subcommands."""
    pass


cli.add_command(run)
cli.add_command(shell)

if __name__ == "__main__":
    cli()
