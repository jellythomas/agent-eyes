from __future__ import annotations

import json

from agent_eyes.cdp import (
    CDPClient,
    _MAX_AX_CONVERSION_NODES,
    _bounded_ax_nodes,
)


def _root_node(*, name: str = "") -> dict:
    node = {
        "nodeId": "root",
        "ignored": False,
        "role": {"type": "role", "value": "RootWebArea"},
        "childIds": [],
        "properties": [],
    }
    if name:
        node["name"] = {"type": "computedString", "value": name}
    return node


def test_ax_builder_caps_node_map_and_signals_truncation():
    node = _root_node()
    nodes = [node] * (_MAX_AX_CONVERSION_NODES + 1)

    tree = CDPClient()._build_tree(nodes)

    assert tree is not None
    assert tree.role == "RootWebArea"
    assert "truncated" in tree.states


def test_ax_byte_budget_stops_before_oversized_node_deterministically():
    root = _root_node()
    root_bytes = len(
        json.dumps(root, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )
    oversized = _root_node(name="x" * 1_000)

    selected, truncated = _bounded_ax_nodes(
        [root, oversized],
        max_nodes=10,
        max_bytes=root_bytes,
    )

    assert selected == [root]
    assert truncated is True


def test_ax_byte_budget_is_not_reported_truncated_at_exact_boundary():
    root = _root_node()
    root_bytes = len(
        json.dumps(root, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )

    selected, truncated = _bounded_ax_nodes(
        [root],
        max_nodes=1,
        max_bytes=root_bytes,
    )

    assert selected == [root]
    assert truncated is False
