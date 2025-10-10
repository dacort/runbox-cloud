from runbox.providers import Provider
from runbox.providers.base import Resource


class AWS(Provider):
    def __init__(self):
        pass

    def run(self, executable, instance_type, options=None):
        import click

        click.echo(f"Running {executable} on AWS with instance type {instance_type}")


class AWSResource(Resource):
    """Base class for AWS resources."""

    provider = "aws"
