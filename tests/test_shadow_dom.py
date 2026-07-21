"""Shadow DOM piercing through CDP DOM.getDocument."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from agent_eyes.cdp import (
    CDPClient,
    ChromeTab,
    _PIERCED_CONTROL_SELECTOR,
    _flatten_dom_nodes,
)
from agent_eyes.js_bridge import merge_pierced_nodes, _dom_to_role, _dom_to_name


class TestDomRoleMapping:
    def test_button_tag(self):
        assert _dom_to_role({"nodeName": "BUTTON", "nodeType": 1, "attributes": []}) == "button"

    def test_anchor_tag(self):
        assert _dom_to_role({"nodeName": "A", "nodeType": 1, "attributes": ["href", "/home"]}) == "link"

    def test_input_text(self):
        assert _dom_to_role({"nodeName": "INPUT", "nodeType": 1, "attributes": ["type", "text"]}) == "textbox"

    def test_input_checkbox(self):
        assert _dom_to_role({"nodeName": "INPUT", "nodeType": 1, "attributes": ["type", "checkbox"]}) == "checkbox"

    def test_aria_role_override(self):
        node = {"nodeName": "DIV", "nodeType": 1, "attributes": ["role", "tab"]}
        assert _dom_to_role(node) == "tab"

    def test_select_tag(self):
        assert _dom_to_role({"nodeName": "SELECT", "nodeType": 1, "attributes": []}) == "combobox"

    def test_text_node_returns_empty(self):
        assert _dom_to_role({"nodeName": "#text", "nodeType": 3, "attributes": []}) == ""


class TestDomNameExtraction:
    def test_aria_label(self):
        node = {"nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "Submit"]}
        assert _dom_to_name(node) == "Submit"

    def test_title_attr(self):
        node = {"nodeName": "A", "nodeType": 1, "attributes": ["title", "Home"]}
        assert _dom_to_name(node) == "Home"

    def test_placeholder(self):
        node = {"nodeName": "INPUT", "nodeType": 1, "attributes": ["placeholder", "Search..."]}
        assert _dom_to_name(node) == "Search..."

    def test_text_content(self):
        node = {"nodeName": "#text", "nodeType": 3, "attributes": [], "nodeValue": "Click me"}
        assert _dom_to_name(node) == "Click me"

    def test_no_name_returns_empty(self):
        node = {"nodeName": "DIV", "nodeType": 1, "attributes": []}
        assert _dom_to_name(node) == ""


class TestMergePiercedNodes:
    def test_adds_shadow_elements(self):
        """Elements from shadow DOM appear in merged result."""
        existing_node_ids = {100, 101, 102}
        pierced_nodes = [
            # Already in AX tree — skip
            {"nodeId": 10, "backendNodeId": 100, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "Menu"]},
            # In shadow DOM — should be added
            {"nodeId": 20, "backendNodeId": 200, "nodeName": "A", "nodeType": 1, "attributes": ["aria-label", "Products", "href", "/products"]},
            {"nodeId": 21, "backendNodeId": 201, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "Settings"]},
            # Text node — should be skipped (not interactive)
            {"nodeId": 202, "nodeName": "#text", "nodeType": 3, "attributes": [], "nodeValue": "some text"},
            # DIV with no role/name — should be skipped
            {"nodeId": 203, "nodeName": "DIV", "nodeType": 1, "attributes": []},
        ]

        shadow_elements = merge_pierced_nodes(pierced_nodes, existing_node_ids)

        assert len(shadow_elements) == 2
        names = [el["name"] for el in shadow_elements]
        assert "Products" in names
        assert "Settings" in names
        roles = [el["role"] for el in shadow_elements]
        assert "link" in roles
        assert "button" in roles
        assert {el["backendNodeId"] for el in shadow_elements} == {200, 201}
        assert all(el["actionable"] is True for el in shadow_elements)

    def test_empty_pierced_returns_empty(self):
        result = merge_pierced_nodes([], set())
        assert result == []


class TestFlattenDomDocument:
    def test_flattens_children_shadow_roots_and_embedded_documents_in_order(self):
        root = {
            "nodeId": 1,
            "children": [
                {
                    "nodeId": 2,
                    "shadowRoots": [
                        {
                            "nodeId": 3,
                            "children": [{"nodeId": 4}],
                        }
                    ],
                }
            ],
            "contentDocument": {"nodeId": 5},
            "templateContent": {"nodeId": 6},
        }

        nodes = _flatten_dom_nodes(root)

        assert [node["nodeId"] for node in nodes] == [1, 2, 3, 4, 5, 6]

    def test_deduplicates_node_ids_and_enforces_hard_cap(self):
        duplicate = {"nodeId": 2}
        root = {
            "nodeId": 1,
            "children": [duplicate, duplicate, {"nodeId": 3}, {"nodeId": 4}],
        }

        nodes = _flatten_dom_nodes(root, max_nodes=3)

        assert [node["nodeId"] for node in nodes] == [1, 2, 3]

    def test_client_caps_named_control_search_before_describing_nodes(self, monkeypatch):
        class Connection:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def run():
            import websockets

            client = CDPClient()
            async def send(_ws, method, params=None, **_kwargs):
                if method == "DOM.performSearch":
                    assert params == {
                        "query": _PIERCED_CONTROL_SELECTOR,
                        "includeUserAgentShadowDOM": True,
                    }
                    return {"searchId": "search-1", "resultCount": 10_000}
                if method == "DOM.getSearchResults":
                    assert params == {
                        "searchId": "search-1",
                        "fromIndex": 0,
                        "toIndex": 2,
                    }
                    return {"nodeIds": [1, 2]}
                if method == "DOM.describeNode":
                    node_id = params["nodeId"]
                    return {
                        "node": {
                            "nodeId": node_id,
                            "backendNodeId": node_id * 10,
                            "nodeType": 1,
                            "nodeName": "BUTTON",
                            "attributes": ["aria-label", f"Button {node_id}"],
                        }
                    }
                return {}

            client._send = AsyncMock(side_effect=send)
            monkeypatch.setattr(
                websockets,
                "connect",
                lambda url, **_kwargs: Connection(),
            )

            nodes = await client.get_pierced_dom(
                ChromeTab(
                    "tab",
                    "Title",
                    "https://example.test",
                    "ws://127.0.0.1:9222/devtools/page/tab",
                ),
                max_nodes=2,
            )

            assert [node["nodeId"] for node in nodes] == [1, 2]
            methods = [call.args[1] for call in client._send.await_args_list]
            assert methods == [
                "DOM.enable",
                "DOM.performSearch",
                "DOM.getSearchResults",
                "DOM.describeNode",
                "DOM.describeNode",
                "DOM.discardSearchResults",
            ]

        asyncio.run(run())


class TestMergePiercedNodesAdditional:
    def test_all_already_in_ax_returns_empty(self):
        pierced = [{"nodeId": 9, "backendNodeId": 1, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "X"]}]
        result = merge_pierced_nodes(pierced, {1})
        assert result == []

    def test_missing_backend_node_id_is_explicitly_non_actionable(self):
        pierced = [
            {
                "nodeId": 9,
                "nodeName": "BUTTON",
                "nodeType": 1,
                "attributes": ["aria-label", "Read only"],
            }
        ]

        result = merge_pierced_nodes(pierced, set())

        assert result == [
            {
                "role": "button",
                "name": "Read only",
                "nodeId": 9,
                "backendNodeId": None,
                "actionable": False,
                "source": "shadow-dom",
            }
        ]
