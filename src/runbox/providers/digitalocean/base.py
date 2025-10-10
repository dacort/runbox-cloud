from providers import Provider
from providers.base import Resource


class DigitalOcean(Provider):
    def __init__(self):
        pass

    def run(self, executable, instance_type, options=None):
        import click

        click.echo(f"Running {executable} on DigitalOcean with size {instance_type}")


class DigitalOceanResource(Resource):
    """Base class for DigitalOcean resources."""

    provider = "digitalocean"
