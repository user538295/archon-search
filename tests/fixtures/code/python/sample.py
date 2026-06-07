"""Sample module docstring for C3c fixture.

Used by tests/test_code_enricher.py to exercise scope-table building.
"""


def top_fn():
    """Top-level function."""
    return 42


class Outer:
    """Outer class with a nested class."""

    class_attr = "outer"

    def outer_method(self):
        """Method on Outer."""
        return self.class_attr

    class Inner:
        """Nested class inside Outer."""

        def inner_method(self):
            """Method on Inner (innermost class wins)."""
            return "inner"


# Module-level statement after Outer (provides a 'module' chunk)
MODULE_CONSTANT = "module_level"


def some_decorator(fn):
    """Simple decorator used below."""
    return fn


@some_decorator
def decorated_fn():
    """Function with a decorator."""
    return "decorated"
