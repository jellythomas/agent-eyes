from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from agent_eyes.adapters.base import AppInfo, UIElement


def _serialized_tool_catalog() -> bytes:
    from agent_eyes.server import TOOLS

    payload = [
        tool.model_dump(mode="json", exclude_none=True)
        for tool in TOOLS
    ]
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def test_tool_catalog_fits_the_16_kib_context_budget():
    assert len(_serialized_tool_catalog()) <= 16 * 1024


def test_transaction_tools_fit_without_raising_context_budget():
    from agent_eyes.server import TOOLS

    names = {tool.name for tool in TOOLS}
    encoded = _serialized_tool_catalog()

    assert {"observe_target", "execute"} <= names
    assert len(encoded) <= 16_128, {"catalog_bytes": len(encoded)}


def test_compact_catalog_preserves_native_first_and_shadow_consent_cues():
    from agent_eyes.server import TOOLS

    by_name = {tool.name: tool for tool in TOOLS}

    for name in ("tree", "list_tabs", "navigate", "new_tab"):
        description = by_name[name].description.lower()
        assert "foreground" in description
        assert "shadow=true" in description

    for name in (
        "web_tree",
        "js",
        "dialog",
        "upload",
        "pierce",
    ):
        description = by_name[name].description.lower()
        assert "shadow=true" in description
        assert "explicit" in description

    explicit_shadow_only = {"web_tree", "js", "dialog", "upload", "pierce"}
    for tool in TOOLS:
        shadow = tool.inputSchema["properties"].get("shadow")
        if shadow is not None:
            if tool.name in explicit_shadow_only:
                assert shadow["enum"] == [True]
                assert "default" not in shadow
            else:
                assert shadow["default"] is False
            description = tool.description.lower()
            assert "shadow=true" in description
            assert "explicit" in description


def test_tree_interactive_output_honors_max_items(monkeypatch):
    from agent_eyes import server

    root = UIElement(
        id=1,
        role="window",
        children=[UIElement(id=index, role="button", name=f"Button {index}") for index in range(2, 7)],
    )
    adapter = MagicMock()
    adapter.get_tree.return_value = root
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    output = asyncio.run(
        server._handle_get_tree({"pid": 9, "interactive_only": True, "max_items": 2})
    )

    assert output.count("button") == 2
    assert "limit 2 reached" in output


def test_context_schema_defaults_to_compact_mode():
    from agent_eyes.server import TOOLS

    context = next(tool for tool in TOOLS if tool.name == "context")

    assert context.inputSchema["properties"]["fast"]["default"] is True


def test_default_context_never_traverses_a_tree(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    adapter.list_apps.return_value = [
        AppInfo(pid=7, name="Notes", windows=["Draft"], is_frontmost=True)
    ]
    adapter.get_focused_element.return_value = None
    adapter.get_tree.side_effect = AssertionError("compact context traversed a tree")
    monkeypatch.setattr(server, "native_adapter", adapter)

    assert server._handle_context({}) == "Notes — Draft"
    adapter.get_tree.assert_not_called()
