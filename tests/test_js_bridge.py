"""Tests for JS-based accessibility tree builder."""
import json
import pytest
from agent_eyes.js_bridge import (
    BUILD_AX_TREE_JS,
    format_ax_tree,
    build_ax_tree_script,
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
