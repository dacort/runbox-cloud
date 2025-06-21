import click
from providers.aws import AWS

@click.group()
def auth():
    pass


@auth.command()
@click.argument("service")
@click.argument("command")
def can_i(service, command):
    aws = AWS()

    if not aws.is_valid_service(service):
        raise click.BadParameter(
            f"Service '{service}' is not available."
        )
    if not aws.is_valid_service_command(service, command):
        raise click.BadParameter(
            f"Command '{command}' is not available for service '{service}'."
        )

    result = aws.can_i(service, command)
    if result:
        return
    else:
        exit(1)