from __future__ import annotations

from types import SimpleNamespace
import sys

from jsonschema import Draft202012Validator, ValidationError
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


def _assert_schema_accepts(schema, arguments):
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(arguments)


def _assert_schema_rejects(schema, arguments):
    Draft202012Validator.check_schema(schema)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(arguments)


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
                "properties": {"files": {"type": "array", "items": {"type": "string"}}},
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
    assert (
        by_name["js"]["properties"]["expression"]["maxLength"] == MAX_JAVASCRIPT_CHARS
    )
    files = by_name["upload"]["properties"]["files"]
    assert files["maxItems"] == MAX_FILES
    assert files["items"]["maxLength"] == MAX_PATH_CHARS
    fields = by_name["fill_form"]["properties"]["fields"]
    assert fields["maxItems"] == MAX_FORM_FIELDS
    assert fields["items"]["properties"]["value"]["maxLength"] == MAX_TYPED_TEXT_CHARS


def test_every_targeted_shadow_tool_accepts_one_bounded_stable_target_id():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}
    targeted_tools = (
        "web_tree",
        "navigate",
        "js",
        "press_key",
        "wait",
        "close_tab",
        "dialog",
        "scroll",
        "drag",
        "pierce",
    )
    if sys.platform == "darwin":
        targeted_tools += ("shadow",)
    assert set(targeted_tools) <= set(by_name)

    for name in targeted_tools:
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
    if sys.platform == "darwin":
        assert "target_id" in by_name["shadow"]["required"]
    else:
        assert "shadow" not in by_name


def test_dual_mode_shadow_contracts_conditionally_require_target_id():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}
    for name in ("navigate", "press_key", "wait", "close_tab", "scroll", "drag"):
        schema = by_name[name]
        assert schema["if"] == {
            "properties": {"shadow": {"enum": [True]}},
            "required": ["shadow"],
        }
        assert schema["then"]["required"] == ["target_id"]
    assert by_name["wait"]["then"]["not"] == {"required": ["pid"]}


def test_effective_schema_depth_limits_match_runtime_caps():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}

    assert by_name["web_tree"]["properties"]["max_depth"]["maximum"] == 10
    assert by_name["subtree"]["properties"]["max_depth"]["maximum"] == 15


def test_effective_schemas_encode_runtime_argument_combinations():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool.inputSchema for tool in TOOLS}

    find = by_name["find"]
    _assert_schema_accepts(find, {"pid": 7, "name": "Save"})
    _assert_schema_accepts(find, {"snapshot": "snap", "role": "button"})
    for arguments in ({}, {"pid": 7}, {"name": "Save"}):
        _assert_schema_rejects(find, arguments)

    click = by_name["click"]
    _assert_schema_accepts(click, {"id": 4})
    _assert_schema_accepts(click, {"x": 10, "y": 20, "pid": 7})
    _assert_schema_accepts(
        click,
        {"id": 4, "snapshot": "shadow-snap", "shadow": True},
    )
    for arguments in (
        {},
        {"x": 10},
        {"y": 20},
        {"x": 10, "y": 20},
        {"id": 4, "shadow": True},
        {"x": 10, "y": 20, "shadow": True},
        {"id": 4, "snapshot": "snap", "x": 10, "y": 20, "pid": 7},
        {
            "id": 4,
            "snapshot": "shadow-snap",
            "x": 10,
            "y": 20,
            "pid": 7,
            "shadow": True,
        },
    ):
        _assert_schema_rejects(click, arguments)

    type_text = by_name["type"]
    _assert_schema_accepts(type_text, {"id": 4, "text": "Ready"})
    _assert_schema_accepts(
        type_text,
        {"id": 4, "text": "Ready", "snapshot": "shadow-snap", "shadow": True},
    )
    _assert_schema_rejects(type_text, {"id": 4, "text": "Ready", "shadow": True})

    wait = by_name["wait"]
    _assert_schema_accepts(wait, {"pid": 7, "role": "button"})
    _assert_schema_accepts(
        wait,
        {"shadow": True, "target_id": "tab-7", "name": "Ready"},
    )
    for arguments in (
        {},
        {"pid": 7},
        {"name": "Ready"},
        {"shadow": True, "target_id": "tab-7"},
        {"shadow": True, "pid": 7, "name": "Ready"},
        {
            "shadow": True,
            "target_id": "tab-7",
            "pid": 7,
            "name": "Ready",
        },
    ):
        _assert_schema_rejects(wait, arguments)

    close_tab = by_name["close_tab"]
    _assert_schema_accepts(close_tab, {"title": "Docs"})
    _assert_schema_accepts(close_tab, {"target_id": "native-tab-7"})
    _assert_schema_accepts(
        close_tab,
        {"shadow": True, "target_id": "shadow-tab-7"},
    )
    for arguments in ({}, {"shadow": True, "title": "Docs"}):
        _assert_schema_rejects(close_tab, arguments)

    hover = by_name["hover"]
    _assert_schema_accepts(hover, {"id": 4, "snapshot": "snap"})
    _assert_schema_accepts(hover, {"x": 10, "y": 20})
    for arguments in (
        {},
        {"id": 4},
        {"x": 10},
        {"y": 20},
        {"id": 4, "snapshot": "snap", "x": 10, "y": 20},
    ):
        _assert_schema_rejects(hover, arguments)

    if "window" in by_name:
        window = by_name["window"]
        _assert_schema_accepts(window, {"action": "list"})
        _assert_schema_accepts(
            window,
            {"action": "focus", "pid": 7, "snapshot": "snap", "id": 4},
        )
        _assert_schema_accepts(
            window,
            {
                "action": "move",
                "pid": 7,
                "snapshot": "snap",
                "id": 4,
                "x": 10,
                "y": 20,
            },
        )
        _assert_schema_accepts(
            window,
            {
                "action": "resize",
                "pid": 7,
                "snapshot": "snap",
                "id": 4,
                "width": 800,
                "height": 600,
            },
        )
        for arguments in (
            {"action": "focus"},
            {"action": "move", "pid": 7, "snapshot": "snap", "id": 4, "x": 10},
            {
                "action": "resize",
                "pid": 7,
                "snapshot": "snap",
                "id": 4,
                "width": 800,
            },
        ):
            _assert_schema_rejects(window, arguments)


def test_effective_descriptions_match_intentional_runtime_behavior():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool for tool in TOOLS}

    assert "requires pid" in by_name["click"].description.lower()
    assert "with or without" in by_name["list_tabs"].description.lower()
    assert "canonical" in by_name["upload"].description.lower()


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
        assert {"app", "window", "shadow"} <= names
        assert len(names) == 30
        window = next(tool for tool in TOOLS if tool.name == "window")
        assert {"snapshot", "id"} <= set(window.inputSchema["properties"])
    else:
        assert "app" not in names
        assert "window" not in names
        assert "shadow" not in names
        assert len(names) == 27


def test_setup_template_uses_the_same_platform_specific_legacy_surface():
    from agent_eyes.setup.templates.mcp_entry import _agent_eyes_tools_for_platform

    macos = set(_agent_eyes_tools_for_platform("darwin"))
    linux = set(_agent_eyes_tools_for_platform("linux"))
    windows = set(_agent_eyes_tools_for_platform("win32"))

    assert "mcp__agent-eyes__shadow" in macos
    assert "mcp__agent-eyes__shadow" not in linux
    assert "mcp__agent-eyes__shadow" not in windows
    assert len(macos) == 29
    assert len(linux) == 26
    assert len(windows) == 26
