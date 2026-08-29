"""Small standard-library validator for the JSON Schema subset used by skills.

This is intentionally not a general JSON Schema implementation. It implements
every keyword committed in the offline WP10 schemas, including the canonical
PolicyDocument schema, and rejects an unknown schema keyword so the validator
cannot silently drift behind them.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

SUPPORTED = {
    "$schema", "$id", "title", "description", "type", "additionalProperties", "required",
    "properties", "items", "enum", "const", "minLength", "maxLength", "minItems", "maxItems",
    "uniqueItems", "minimum", "maximum", "exclusiveMinimum", "anyOf", "oneOf", "pattern",
    "format", "$defs", "$ref", "default", "discriminator",
}


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }.get(expected, False)


def _format_matches(value: str, expected: str) -> bool:
    if expected == "uuid":
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            return False
        return True
    if expected == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return "T" in value
    if expected == "email":
        return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None
    return False


def _resolve_ref(reference: str, root_schema: dict[str, Any]) -> dict[str, Any] | None:
    if not reference.startswith("#/"):
        return None
    current: Any = root_schema
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, dict) else None


def validate(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
    *,
    root_schema: dict[str, Any] | None = None,
) -> list[str]:
    root_schema = schema if root_schema is None else root_schema
    unknown_keywords = set(schema) - SUPPORTED
    if unknown_keywords:
        return [f"{path}: validator does not implement schema keyword {key!r}" for key in sorted(unknown_keywords)]
    failures: list[str] = []
    if "$ref" in schema:
        referenced = _resolve_ref(schema["$ref"], root_schema)
        if referenced is None:
            failures.append(f"{path}: unsupported or missing schema reference {schema['$ref']!r}")
        else:
            failures.extend(validate(instance, referenced, path, root_schema=root_schema))
    if "anyOf" in schema:
        branches = [validate(instance, branch, path, root_schema=root_schema) for branch in schema["anyOf"]]
        if not any(not branch for branch in branches):
            failures.append(f"{path}: value matches no anyOf branch")
    if "oneOf" in schema:
        branches = [validate(instance, branch, path, root_schema=root_schema) for branch in schema["oneOf"]]
        if sum(not branch for branch in branches) != 1:
            failures.append(f"{path}: value must match exactly one oneOf branch")
    if "const" in schema and instance != schema["const"]:
        failures.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        failures.append(f"{path}: value is not in the allowed enum")
    expected_types = schema.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_type_matches(instance, item) for item in expected_types):
            failures.append(f"{path}: expected type {' or '.join(expected_types)}")
            return failures
    if isinstance(instance, dict):
        required = set(schema.get("required", []))
        for key in sorted(required - set(instance)):
            failures.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                failures.append(f"{path}: unknown property {key!r}")
        for key, value in instance.items():
            if key in properties:
                failures.extend(validate(value, properties[key], f"{path}.{key}", root_schema=root_schema))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            failures.append(f"{path}: has fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            failures.append(f"{path}: has more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(serialized) != len(set(serialized)):
                failures.append(f"{path}: items must be unique")
        item_schema = schema.get("items")
        if item_schema:
            for index, value in enumerate(instance):
                failures.extend(validate(value, item_schema, f"{path}[{index}]", root_schema=root_schema))
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            failures.append(f"{path}: is shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            failures.append(f"{path}: is longer than {schema['maxLength']} characters")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            failures.append(f"{path}: does not match the required pattern")
        if "format" in schema and not _format_matches(instance, schema["format"]):
            failures.append(f"{path}: does not match format {schema['format']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            failures.append(f"{path}: is below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            failures.append(f"{path}: is above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            failures.append(f"{path}: is not above exclusive minimum {schema['exclusiveMinimum']}")
    return failures
