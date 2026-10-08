"""A small JSON Schema validator for tool arguments.

Covers the subset the tool schemas use: object/string/integer/boolean/array
types (or a list of them), enum, required, additionalProperties: false,
items, minimum/maximum, and minLength. Errors are written for the model to
read and fix.
"""
from __future__ import annotations

_PY_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _is_type(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _PY_TYPES[name])


def validate(value, schema: dict, path: str = "arguments") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, name) for name in names):
            return [f"{path} must be {' or '.join(names)}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of: {', '.join(str(v) for v in schema['enum'])}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path} must not be empty")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} must be at most {schema['maximum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is required")
        for key, item in value.items():
            if key in properties:
                errors.extend(validate(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                allowed = ", ".join(properties) or "none"
                errors.append(f"{path}.{key} is not a known argument (allowed: {allowed})")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors
