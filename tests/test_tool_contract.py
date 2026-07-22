from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

from agent_eyes.tool_contract import (
    MAX_FILES,
    MAX_FORM_FIELDS,
    MAX_JAVASCRIPT_CHARS,
    MAX_PATH_CHARS,
    MAX_TYPED_TEXT_CHARS,
    harden_tool_schemas,
)


def _walk(schema, path="arguments"):
    yield path, schema
    if schema.get("type") == "object":
        for name, child in schema.get("properties", {}).items():
            yield from _walk(child, f"{path}.{name}")
    elif schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], f"{path}[]")


def test_hardens_every_supported_schema_leaf():
    tool = SimpleNamespace(
        name="sample",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "values": {"type": "array", "items": {"type": "string"}},
                "nested": {
                    "type": "object",
                    "properties": {"flag": {"type": "boolean"}},
                },
            },
        },
    )

    harden_tool_schemas([tool])

    for _, schema in _walk(tool.inputSchema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
        elif schema.get("type") == "array":
            assert schema["maxItems"] > 0
        elif schema.get("type") == "string":
            assert schema["maxLength"] > 0
        elif schema.get("type") in {"integer", "number"}:
            assert schema["minimum"] <= schema["maximum"]


def test_sensitive_operation_limits_are_explicit():
    tools = [
        SimpleNamespace(
            name="type",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        ),
        SimpleNamespace(
            name="js",
            inputSchema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
            },
        ),
        SimpleNamespace(
            name="upload",
            inputSchema={
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}}
                },
            },
        ),
        SimpleNamespace(
            name="fill_form",
            inputSchema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    }
                },
            },
        ),
    ]

    harden_tool_schemas(tools)

    by_name = {tool.name: tool.inputSchema for tool in tools}
    assert by_name["type"]["properties"]["text"]["maxLength"] == MAX_TYPED_TEXT_CHARS
    assert by_name["js"]["properties"]["expression"]["maxLength"] == MAX_JAVASCRIPT_CHARS
    files = by_name["upload"]["properties"]["files"]
    assert files["maxItems"] == MAX_FILES
    assert files["items"]["maxLength"] == MAX_PATH_CHARS
    fields = by_name["fill_form"]["properties"]["fields"]
    assert fields["maxItems"] == MAX_FORM_FIELDS
    assert fields["items"]["properties"]["value"]["maxLength"] == MAX_TYPED_TEXT_CHARS


def test_every_targeted_shadow_tool_accepts_one_bounded_stable_target_id():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}
    for name in (
        "web_tree",
        "navigate",
        "js",
        "press_key",
        "wait",
        "close_tab",
        "dialog",
        "scroll",
        "shadow",
        "drag",
        "pierce",
    ):
        target = by_name[name]["properties"]["target_id"]
        assert target["type"] == "string"
        assert target["maxLength"] == 512
        assert "tab_index" not in by_name[name]["properties"]

    upload = by_name["upload"]
    assert upload["properties"]["snapshot"]["maxLength"] == 128
    assert "snapshot" in upload["required"]
    for name in ("fill_form", "subtree"):
        assert by_name[name]["properties"]["snapshot"]["maxLength"] == 128
        assert "snapshot" in by_name[name]["required"]


def test_shadow_only_contracts_require_explicit_mode_and_target():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}
    for name in ("web_tree", "js", "dialog", "pierce"):
        schema = by_name[name]
        assert {"shadow", "target_id"} <= set(schema["required"])
        assert schema["properties"]["shadow"]["enum"] == [True]
        assert "default" not in schema["properties"]["shadow"]

    upload = by_name["upload"]
    assert "shadow" in upload["required"]
    assert upload["properties"]["shadow"]["enum"] == [True]
    assert "target_id" in by_name["shadow"]["required"]


def test_dual_mode_shadow_contracts_conditionally_require_target_id():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}
    for name in ("navigate", "press_key", "wait", "close_tab", "scroll", "drag"):
        schema = by_name[name]
        assert schema["if"] == {
            "properties": {"shadow": {"enum": [True]}},
            "required": ["shadow"],
        }
        assert schema["then"] == {"required": ["target_id"]}


def test_preserves_stricter_existing_bounds_and_rejects_invalid_tools():
    tool = SimpleNamespace(
        name="bounded",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "string", "maxLength": 10}},
        },
    )
    harden_tool_schemas([tool])
    assert tool.inputSchema["properties"]["value"]["maxLength"] == 10

    with pytest.raises(TypeError):
        harden_tool_schemas([SimpleNamespace(name="bad", inputSchema=None)])


def test_catalog_only_advertises_platform_supported_app_and_window_tools():
    from agent_eyes.server import TOOLS

    names = {tool.name for tool in TOOLS}
    if sys.platform == "darwin":
        assert {"app", "window"} <= names
        window = next(tool for tool in TOOLS if tool.name == "window")
        assert {"snapshot", "id"} <= set(window.inputSchema["properties"])
    else:
        assert "app" not in names
        assert "window" not in names
