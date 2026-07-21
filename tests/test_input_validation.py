from __future__ import annotations

import pytest

from agent_eyes.input_validation import (
    InputValidationError,
    validate_tool_arguments,
)


SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 4},
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "timeout": {"type": "number", "minimum": 0, "maximum": 5},
        "enabled": {"type": "boolean"},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
            "items": {
                "type": "object",
                "properties": {"value": {"type": "string", "maxLength": 3}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["text", "count"],
    "additionalProperties": False,
}


def test_valid_nested_arguments_pass():
    validate_tool_arguments(
        SCHEMA,
        {
            "text": "four",
            "count": 2,
            "timeout": 0.5,
            "enabled": False,
            "items": [{"value": "one"}, {"value": "two"}],
        },
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"count": 1}, "arguments.text is required"),
        ({"text": "secret", "count": 1}, "at most 4 characters"),
        ({"text": "ok", "count": True}, "arguments.count must be integer"),
        ({"text": "ok", "count": 4}, "arguments.count must be at most 3"),
        ({"text": "ok", "count": 1, "timeout": float("nan")}, "finite JSON"),
        (
            {"text": "ok", "count": 1, "items": []},
            "at least 1 items",
        ),
        (
            {"text": "ok", "count": 1, "items": [{"value": "same"}]},
            "at most 3 characters",
        ),
        (
            {"text": "ok", "count": 1, "unknown": "value"},
            "arguments.unknown is not allowed",
        ),
    ],
)
def test_invalid_arguments_fail_with_path_but_without_caller_value(arguments, message):
    with pytest.raises(InputValidationError, match=message) as exc_info:
        validate_tool_arguments(SCHEMA, arguments)

    assert "secret" not in str(exc_info.value)


def test_duplicate_nested_items_fail():
    with pytest.raises(InputValidationError, match="unique"):
        validate_tool_arguments(
            SCHEMA,
            {
                "text": "ok",
                "count": 1,
                "items": [{"value": "one"}, {"value": "one"}],
            },
        )


def test_utf8_serialized_argument_ceiling_is_enforced():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": 100}},
    }

    with pytest.raises(InputValidationError, match="20-byte"):
        validate_tool_arguments(schema, {"text": "🙂🙂🙂"}, byte_limit=20)


def test_non_json_and_recursive_arguments_fail_closed():
    recursive: dict = {}
    recursive["self"] = recursive

    for arguments in ({"value": object()}, recursive):
        with pytest.raises(InputValidationError, match="finite JSON"):
            validate_tool_arguments({"type": "object"}, arguments)
