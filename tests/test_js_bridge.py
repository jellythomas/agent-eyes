"""Tests for JS-based accessibility tree builder."""
import json
import pytest
from agent_eyes.js_bridge import (
    BUILD_AX_TREE_JS,
    _dom_to_name,
    format_ax_tree,
    build_ax_tree_script,
    merge_pierced_nodes,
)


class TestBuildAxTreeScript:
    def test_script_is_valid_javascript(self):
        """The JS constant should be a non-empty string."""
        assert isinstance(BUILD_AX_TREE_JS, str)
        assert len(BUILD_AX_TREE_JS) > 100
        assert "buildAccessibilityTree" in BUILD_AX_TREE_JS

    def test_script_returns_json_call(self):
        """build_ax_tree_script wraps in JSON.stringify."""
        script = build_ax_tree_script(max_depth=3)
        assert "JSON.stringify" in script
        assert "maxDepth" in script or "3" in script

    def test_script_has_a_secure_value_guard(self):
        assert "isSecure" in BUILD_AX_TREE_JS
        assert "type === 'password'" in BUILD_AX_TREE_JS

    def test_script_caps_total_observed_nodes(self):
        assert "nextId > 500" in BUILD_AX_TREE_JS


class TestFormatAxTree:
    def test_formats_simple_tree(self):
        tree = {
            "role": "main",
            "name": "Content",
            "bounds": [0, 0, 800, 600],
            "interactive": False,
            "children": [
                {
                    "role": "button",
                    "name": "Submit",
                    "bounds": [10, 10, 100, 40],
                    "interactive": True,
                    "children": [],
                }
            ],
        }
        result = format_ax_tree(tree)
        assert "main" in result
        assert "Submit" in result
        assert "button" in result

    def test_formats_empty_tree(self):
        result = format_ax_tree(None)
        assert "empty" in result.lower() or "no " in result.lower()

    def test_assigns_element_ids(self):
        tree = {
            "role": "button",
            "name": "Click me",
            "bounds": [0, 0, 100, 40],
            "interactive": True,
            "children": [],
        }
        result = format_ax_tree(tree)
        assert "[" in result  # Should have element IDs like [1]

    def test_secure_node_value_is_never_formatted(self):
        secret = "javascript-tree-secret-aa19"
        tree = {
            "id": 1,
            "role": "textbox",
            "name": "Password",
            "value": secret,
            "secure": True,
            "interactive": True,
            "children": [],
        }

        result = format_ax_tree(tree)

        assert secret not in result
        assert "value=" not in result
        assert "secure" in result

    def test_password_role_value_is_redacted_without_secure_metadata(self):
        secret = "legacy-password-node-secret-c752"
        tree = {
            "id": 1,
            "role": "password text",
            "name": "Password",
            "value": secret,
            "interactive": True,
            "children": [],
        }

        result = format_ax_tree(tree)

        assert secret not in result
        assert "value=" not in result


class TestPiercedDomSecureValues:
    def test_password_value_attribute_is_not_an_accessible_name(self):
        node = {
            "nodeName": "INPUT",
            "nodeType": 1,
            "attributes": ["type", "password", "value", "dom-secret-24ab"],
        }

        assert _dom_to_name(node) == ""

    @pytest.mark.parametrize(
        "attributes",
        [
            ["type", "text", "autocomplete", "current-password", "value", "semantic-secret"],
            ["type", "text", "autocomplete", "section-login new-password", "value", "new-secret"],
            ["type", "text", "style", "-webkit-text-security: disc", "value", "styled-secret"],
        ],
    )
    def test_semantically_or_visually_secure_value_is_not_a_name(self, attributes):
        node = {
            "nodeName": "INPUT",
            "nodeType": 1,
            "attributes": attributes,
        }

        assert _dom_to_name(node) == ""

    @pytest.mark.parametrize(
        "style",
        [
            "-webkit-text-security: none !important",
            "-webkit-text-security: disc; -webkit-text-security: none",
        ],
    )
    def test_explicitly_unsecured_inline_style_preserves_value_name(self, style):
        node = {
            "nodeName": "INPUT",
            "nodeType": 1,
            "attributes": ["type", "text", "style", style, "value", "visible value"],
        }

        assert _dom_to_name(node) == "visible value"

    def test_password_shadow_node_keeps_label_but_never_value(self):
        secret = "shadow-password-secret-00ff"
        node = {
            "nodeId": 7,
            "backendNodeId": 70,
            "nodeName": "INPUT",
            "nodeType": 1,
            "attributes": [
                "type",
                "password",
                "aria-label",
                "Account password",
                "value",
                secret,
            ],
        }

        result = merge_pierced_nodes([node], set())

        assert len(result) == 1
        assert result[0]["name"] == "Account password"
        assert result[0]["secure"] is True
        assert secret not in repr(result)
