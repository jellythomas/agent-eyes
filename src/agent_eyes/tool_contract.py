"""Central size/range limits for the public MCP tool contract."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar


MAX_TYPED_TEXT_CHARS = 16_384
MAX_JAVASCRIPT_CHARS = 65_536
MAX_URL_CHARS = 8_192
MAX_SELECTOR_CHARS = 2_048
MAX_QUERY_CHARS = 512
MAX_PATH_CHARS = 4_096
MAX_FILES = 32
MAX_FORM_FIELDS = 100

_SHADOW_TARGET_TOOLS = frozenset(
    {
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
    }
)

_EXPLICIT_SHADOW_ONLY_TOOLS = frozenset(
    {
        "web_tree",
        "js",
        "dialog",
        "upload",
        "pierce",
    }
)

_REQUIRED_SHADOW_TARGET_TOOLS = frozenset(
    {
        "web_tree",
        "js",
        "dialog",
        "shadow",
        "pierce",
    }
)

_CONDITIONAL_SHADOW_TARGET_TOOLS = _SHADOW_TARGET_TOOLS - _REQUIRED_SHADOW_TARGET_TOOLS

_REQUIRED_SNAPSHOT_TOOLS = frozenset({"upload", "fill_form", "subtree"})


_T = TypeVar("_T")

_STRING_LIMITS = {
    "type.text": MAX_TYPED_TEXT_CHARS,
    "js.expression": MAX_JAVASCRIPT_CHARS,
    "shadow.text": MAX_JAVASCRIPT_CHARS,
    "navigate.url": MAX_URL_CHARS,
    "new_tab.url": MAX_URL_CHARS,
    "list_tabs.query": MAX_QUERY_CHARS,
    "navigate.query": MAX_QUERY_CHARS,
    "new_tab.query": MAX_QUERY_CHARS,
    "find.name": MAX_QUERY_CHARS,
    "find.value": MAX_QUERY_CHARS,
    "wait.name": MAX_QUERY_CHARS,
    "close_tab.title": MAX_QUERY_CHARS,
    "shadow.selector": MAX_SELECTOR_CHARS,
    "pierce.selector": MAX_SELECTOR_CHARS,
    "dialog.prompt_text": 4_096,
    "fill_form.fields[].value": MAX_TYPED_TEXT_CHARS,
    "upload.files[]": MAX_PATH_CHARS,
}

_ARRAY_LIMITS = {
    "press_key.modifiers": 4,
    "upload.files": MAX_FILES,
    "fill_form.fields": MAX_FORM_FIELDS,
}

_STRING_PROPERTY_LIMITS = {
    "target_id": 512,
    "snapshot": 128,
    "key": 64,
    "role": 128,
}

_INTEGER_LIMITS = {
    "pid": (1, 2_147_483_647),
    "id": (0, 2_147_483_647),
    "max_depth": (0, 20),
    "web_tree.max_depth": (0, 10),
    "subtree.max_depth": (0, 15),
    "max_items": (1, 200),
    "max_results": (1, 50),
    "tab_index": (0, 100_000),
    "shadow.tab_index": (-1, 100_000),
    "amount": (-1_000_000, 1_000_000),
    "delta_x": (-1_000_000, 1_000_000),
    "delta_y": (-1_000_000, 1_000_000),
    "x": (-1_000_000, 1_000_000),
    "y": (-1_000_000, 1_000_000),
    "from_x": (-1_000_000, 1_000_000),
    "from_y": (-1_000_000, 1_000_000),
    "to_x": (-1_000_000, 1_000_000),
    "to_y": (-1_000_000, 1_000_000),
    "width": (1, 1_000_000),
    "height": (1, 1_000_000),
}


def _required_nonempty_string(property_name: str) -> dict[str, Any]:
    return {
        "properties": {property_name: {"minLength": 1}},
        "required": [property_name],
    }


def align_runtime_argument_contracts(tools: Iterable[_T]) -> list[_T]:
    """Encode handler-required argument combinations in public schemas."""
    aligned = list(tools)
    for tool in aligned:
        name = getattr(tool, "name", "")
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise TypeError("tools must expose string name and dictionary inputSchema")

        if name == "find":
            schema["allOf"] = [
                {
                    "anyOf": [
                        _required_nonempty_string("role"),
                        _required_nonempty_string("name"),
                        _required_nonempty_string("value"),
                    ]
                },
                {
                    "anyOf": [
                        {"required": ["pid"]},
                        _required_nonempty_string("snapshot"),
                    ]
                },
            ]
        elif name == "click":
            schema["if"] = {
                "properties": {"shadow": {"enum": [True]}},
                "required": ["shadow"],
            }
            schema["then"] = {
                "required": ["id", "snapshot"],
                "not": {
                    "anyOf": [
                        {"required": ["x"]},
                        {"required": ["y"]},
                        {"required": ["pid"]},
                    ]
                },
            }
            schema["else"] = {
                "anyOf": [
                    {
                        "required": ["id"],
                        "not": {
                            "anyOf": [
                                {"required": ["x"]},
                                {"required": ["y"]},
                                {"required": ["pid"]},
                            ]
                        },
                    },
                    {
                        "required": ["x", "y", "pid"],
                        "not": {
                            "anyOf": [
                                {"required": ["id"]},
                                {"required": ["snapshot"]},
                            ]
                        },
                    },
                ]
            }
        elif name == "type":
            schema["if"] = {
                "properties": {"shadow": {"enum": [True]}},
                "required": ["shadow"],
            }
            schema["then"] = {"required": ["snapshot"]}
        elif name == "wait":
            schema["allOf"] = [
                {
                    "anyOf": [
                        _required_nonempty_string("role"),
                        _required_nonempty_string("name"),
                    ]
                }
            ]
            schema["then"] = {
                "required": ["target_id"],
                "not": {"required": ["pid"]},
            }
            schema["else"] = {
                "required": ["pid"],
                "not": {"required": ["target_id"]},
            }
        elif name == "close_tab":
            schema["else"] = {
                "anyOf": [
                    _required_nonempty_string("target_id"),
                    _required_nonempty_string("title"),
                ]
            }
        elif name == "hover":
            schema["anyOf"] = [
                {
                    "required": ["id", "snapshot"],
                    "not": {
                        "anyOf": [
                            {"required": ["x"]},
                            {"required": ["y"]},
                        ]
                    },
                },
                {
                    "required": ["x", "y"],
                    "not": {
                        "anyOf": [
                            {"required": ["id"]},
                            {"required": ["snapshot"]},
                        ]
                    },
                },
            ]
        elif name == "window":
            schema["allOf"] = [
                {
                    "if": {
                        "properties": {
                            "action": {
                                "enum": ["focus", "minimize", "close", "move", "resize"]
                            }
                        },
                        "required": ["action"],
                    },
                    "then": {"required": ["pid", "snapshot", "id"]},
                },
                {
                    "if": {
                        "properties": {"action": {"enum": ["move"]}},
                        "required": ["action"],
                    },
                    "then": {"required": ["x", "y"]},
                },
                {
                    "if": {
                        "properties": {"action": {"enum": ["resize"]}},
                        "required": ["action"],
                    },
                    "then": {"required": ["width", "height"]},
                },
            ]
    return aligned


def harden_tool_schemas(tools: Iterable[_T]) -> list[_T]:
    """Apply deterministic limits to every schema leaf in-place."""
    hardened = list(tools)
    for tool in hardened:
        name = getattr(tool, "name", "")
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise TypeError("tools must expose string name and dictionary inputSchema")
        _harden_schema(schema, path=name, property_name="")
    return hardened


def expose_shadow_target_ids(tools: Iterable[_T]) -> list[_T]:
    """Expose canonical targeting and snapshot qualification where required."""
    prepared = list(tools)
    for tool in prepared:
        name = getattr(tool, "name", "")
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict):
            raise TypeError("tools must expose dictionary inputSchema")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise TypeError("tool object schemas must expose properties")
        if name in _SHADOW_TARGET_TOOLS:
            properties.setdefault(
                "target_id",
                {
                    "type": "string",
                    "description": "Stable shadow target ID from list_tabs",
                },
            )
        if name in {"find", "upload", "fill_form", "hover", "subtree"}:
            properties.setdefault(
                "snapshot",
                {
                    "type": "string",
                    "description": "Snapshot token returned by tree/web_tree",
                },
            )
        required = schema.setdefault("required", [])
        if name in _REQUIRED_SNAPSHOT_TOOLS and "snapshot" not in required:
            required.append("snapshot")
        if name in _REQUIRED_SHADOW_TARGET_TOOLS and "target_id" not in required:
            required.append("target_id")
        if name in _CONDITIONAL_SHADOW_TARGET_TOOLS:
            schema["if"] = {
                "properties": {"shadow": {"enum": [True]}},
                "required": ["shadow"],
            }
            schema["then"] = {"required": ["target_id"]}
        if name in _EXPLICIT_SHADOW_ONLY_TOOLS:
            shadow = properties.get("shadow")
            if not isinstance(shadow, dict):
                raise TypeError(f"{name} must expose a shadow property")
            shadow.pop("default", None)
            shadow["enum"] = [True]
            if "shadow" not in required:
                required.append("shadow")
    return prepared


def _harden_schema(
    schema: dict[str, Any],
    *,
    path: str,
    property_name: str,
) -> None:
    schema_type = schema.get("type")
    if schema_type == "object":
        schema.setdefault("additionalProperties", False)
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                if isinstance(child, dict):
                    _harden_schema(
                        child,
                        path=f"{path}.{name}",
                        property_name=name,
                    )
        return

    if schema_type == "array":
        schema.setdefault("maxItems", _ARRAY_LIMITS.get(path, 100))
        items = schema.get("items")
        if isinstance(items, dict):
            _harden_schema(
                items,
                path=f"{path}[]",
                property_name=property_name,
            )
        return

    if schema_type == "string":
        schema.setdefault(
            "maxLength",
            _STRING_LIMITS.get(
                path,
                _STRING_PROPERTY_LIMITS.get(property_name, 4_096),
            ),
        )
        return

    if schema_type == "integer":
        minimum, maximum = _INTEGER_LIMITS.get(
            path,
            _INTEGER_LIMITS.get(property_name, (-2_147_483_648, 2_147_483_647)),
        )
        schema.setdefault("minimum", minimum)
        schema.setdefault("maximum", maximum)
        return

    if schema_type == "number":
        if property_name == "timeout":
            schema.setdefault("minimum", 0)
            schema.setdefault("maximum", 60)
        else:
            schema.setdefault("minimum", -1_000_000)
            schema.setdefault("maximum", 1_000_000)
