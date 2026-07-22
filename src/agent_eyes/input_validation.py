"""Dependency-free runtime validation for MCP tool arguments."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any


MAX_ARGUMENT_BYTES = 256 * 1024


class InputValidationError(ValueError):
    """A compact validation failure that never includes caller values."""


def validate_tool_arguments(
    schema: Mapping[str, Any],
    arguments: Any,
    *,
    byte_limit: int = MAX_ARGUMENT_BYTES,
) -> None:
    """Validate the supported JSON-Schema subset at the trust boundary."""
    if (
        isinstance(byte_limit, bool)
        or not isinstance(byte_limit, int)
        or byte_limit < 1
    ):
        raise ValueError("byte_limit must be a positive integer")
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InputValidationError("arguments must be finite JSON data") from exc
    if len(encoded) > byte_limit:
        raise InputValidationError(
            f"arguments exceed the {byte_limit}-byte request limit"
        )
    _validate(schema, arguments, path="arguments")


def _validate(schema: Mapping[str, Any], value: Any, *, path: str) -> None:
    if "not" in schema:
        negated = schema["not"]
        if not isinstance(negated, Mapping):
            raise ValueError("not must be a schema")
        if _matches(negated, value):
            raise InputValidationError(f"{path} matches a forbidden argument shape")

    conditional = schema.get("if")
    if isinstance(conditional, Mapping):
        branch = schema.get("then") if _matches(conditional, value) else schema.get("else")
        if isinstance(branch, Mapping):
            _validate(branch, value, path=path)

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if not isinstance(branch, Mapping):
                raise ValueError("allOf entries must be schemas")
            _validate(branch, value, path=path)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        if not any_of or not all(isinstance(branch, Mapping) for branch in any_of):
            raise ValueError("anyOf must contain only schemas")
        if not any(_matches(branch, value) for branch in any_of):
            raise InputValidationError(f"{path} does not match an allowed argument shape")

    expected = schema.get("type")
    if expected is not None:
        _validate_type(expected, value, path=path)

    if "enum" in schema and value not in schema["enum"]:
        raise InputValidationError(f"{path} is not an allowed value")

    if isinstance(value, str):
        _validate_length(schema, len(value), path=path, unit="characters")
    elif isinstance(value, list):
        _validate_length(schema, len(value), path=path, unit="items")
        if schema.get("uniqueItems"):
            serialized = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(serialized) != len(set(serialized)):
                raise InputValidationError(f"{path} must contain unique items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item_schema, item, path=f"{path}[{index}]")
    elif isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise InputValidationError(f"{path}.{name} is required")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for name, item in value.items():
                item_schema = properties.get(name)
                if isinstance(item_schema, Mapping):
                    _validate(item_schema, item, path=f"{path}.{name}")
                elif schema.get("additionalProperties") is False:
                    raise InputValidationError(f"{path}.{name} is not allowed")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise InputValidationError(f"{path} must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise InputValidationError(f"{path} must be at most {maximum}")


def _matches(schema: Mapping[str, Any], value: Any) -> bool:
    try:
        _validate(schema, value, path="condition")
    except InputValidationError:
        return False
    return True


def _validate_type(expected: Any, value: Any, *, path: str) -> None:
    valid = False
    if expected == "object":
        valid = isinstance(value, dict)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "null":
        valid = value is None
    else:
        raise ValueError(f"unsupported schema type: {expected!r}")
    if not valid:
        raise InputValidationError(f"{path} must be {expected}")


def _validate_length(
    schema: Mapping[str, Any],
    length: int,
    *,
    path: str,
    unit: str,
) -> None:
    minimum_key = "minLength" if unit == "characters" else "minItems"
    maximum_key = "maxLength" if unit == "characters" else "maxItems"
    minimum = schema.get(minimum_key)
    maximum = schema.get(maximum_key)
    if minimum is not None and length < minimum:
        raise InputValidationError(
            f"{path} must contain at least {minimum} {unit}"
        )
    if maximum is not None and length > maximum:
        raise InputValidationError(
            f"{path} must contain at most {maximum} {unit}"
        )
