"""Shadow DOM piercing via CDP DOM.getFlattenedDocument."""
import pytest
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
            {"nodeId": 100, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "Menu"]},
            # In shadow DOM — should be added
            {"nodeId": 200, "nodeName": "A", "nodeType": 1, "attributes": ["aria-label", "Products", "href", "/products"]},
            {"nodeId": 201, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "Settings"]},
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

    def test_empty_pierced_returns_empty(self):
        result = merge_pierced_nodes([], set())
        assert result == []

    def test_all_already_in_ax_returns_empty(self):
        pierced = [{"nodeId": 1, "nodeName": "BUTTON", "nodeType": 1, "attributes": ["aria-label", "X"]}]
        result = merge_pierced_nodes(pierced, {1})
        assert result == []
