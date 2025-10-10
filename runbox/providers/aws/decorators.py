def depends_on(*resources):
    """Class decorator to declare resource dependencies."""

    def decorator(cls):
        cls.depends_on = list(resources)
        return cls

    return decorator
