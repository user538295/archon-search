"""Minimal JSON schema validator (subset: type, required, properties)."""
from __future__ import annotations
from typing import Any


class ValidationError(Exception):
    pass


def validate(instance: Any, schema: dict) -> None:
    """Validate *instance* against *schema*.

    Supported keywords: type, required, properties, items, minimum, maximum.
    Raises :class:`ValidationError` on failure.
    """
    _check_type(instance, schema)
    if "required" in schema and isinstance(instance, dict):
        for field in schema["required"]:
            if field not in instance:
                raise ValidationError(f"Missing required field: {field!r}")
    if "properties" in schema and isinstance(instance, dict):
        for key, sub_schema in schema["properties"].items():
            if key in instance:
                validate(instance[key], sub_schema)
    if "items" in schema and isinstance(instance, list):
        for i, item in enumerate(instance):
            validate(item, schema["items"])
    if "minimum" in schema and isinstance(instance, (int, float)):
        if instance < schema["minimum"]:
            raise ValidationError(f"{instance} < minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(instance, (int, float)):
        if instance > schema["maximum"]:
            raise ValidationError(f"{instance} > maximum {schema['maximum']}")


def _check_type(instance: Any, schema: dict) -> None:
    TYPE_MAP = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "array": list, "object": dict, "null": type(None),
    }
    if "type" not in schema:
        return
    expected = schema["type"]
    cls = TYPE_MAP.get(expected)
    if cls and not isinstance(instance, cls):
        raise ValidationError(f"Expected {expected}, got {type(instance).__name__}")
