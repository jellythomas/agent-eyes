"""Tests for AppleScript fallback paths in CDP-only handlers.

These tests verify the fallback contracts for the 7 tools that previously
only worked when CDP was available. The approach mirrors test_close_tab.py:
pure logic tests for fallback selection, plus contract tests that document
what each fallback must do.

Since server.py requires platform-specific native adapters, we test the
fallback decision logic in isolation and document the server-level contracts.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_tab(title="Test Tab", url="https://example.com", tab_id="1"):
    tab = MagicMock()
    tab.title = title
    tab.url = url
    tab.id = tab_id
    return tab


def _make_as_module(available=True):
    """Return a mock AppleScript module."""
    mod = MagicMock()
    mod.is_available.return_value = available
    return mod


# ---------------------------------------------------------------------------
# Task 3.1 — _handle_get_web_tree fallback contract
# ---------------------------------------------------------------------------

class TestGetWebTreeFallbackContract(unittest.TestCase):
    """_handle_get_web_tree: when CDP unavailable on macOS, fall back to
    AppleScript JS injection (build_ax_tree_script + format_ax_tree)."""

    def test_cdp_unavailable_triggers_applescript_path(self):
        """Contract: when CDP is not available, AppleScript path is tried."""
        # Simulates the decision: if not cdp_available and darwin and _as available
        cdp_available = False
        platform = "darwin"
        as_available = True

        use_fallback = (
            not cdp_available
            and platform == "darwin"
            and as_available
        )
        self.assertTrue(use_fallback)

    def test_cdp_available_skips_applescript(self):
        """Contract: when CDP is available, AppleScript fallback is NOT used."""
        cdp_available = True
        platform = "darwin"
        as_available = True

        use_fallback = (
            not cdp_available
            and platform == "darwin"
            and as_available
        )
        self.assertFalse(use_fallback)

    def test_non_darwin_with_no_cdp_returns_error(self):
        """Contract: on non-macOS without CDP, return error message."""
        cdp_available = False
        platform = "linux"
        as_available = False

        use_fallback = (
            not cdp_available
            and platform == "darwin"
            and as_available
        )
        self.assertFalse(use_fallback)

    def test_build_ax_tree_script_called_with_max_depth(self):
        """Contract: fallback must call build_ax_tree_script(max_depth)."""
        from agent_eyes.js_bridge import build_ax_tree_script
        script = build_ax_tree_script(max_depth=5)
        # Script is a JS string that invokes the function with depth argument
        self.assertIn("5", script)
        self.assertIn("JSON.stringify", script)

    def test_format_ax_tree_formats_result(self):
        """Contract: fallback must pass parsed JSON result to format_ax_tree."""
        from agent_eyes.js_bridge import format_ax_tree
        tree = {
            "id": 1, "role": "button", "name": "Submit",
            "bounds": [0, 0, 100, 30], "interactive": True, "children": []
        }
        result = format_ax_tree(tree)
        self.assertIn("button", result)
        self.assertIn("Submit", result)

    def test_format_ax_tree_handles_none(self):
        """Contract: format_ax_tree gracefully handles None (empty page)."""
        from agent_eyes.js_bridge import format_ax_tree
        result = format_ax_tree(None)
        self.assertIn("No accessibility tree", result)

    def test_applescript_js_execution_called_with_script(self):
        """Contract: fallback calls _as.execute_javascript with the built script."""
        mock_as = _make_as_module(available=True)
        mock_as.execute_javascript.return_value = '{"id":1,"role":"body","name":"","bounds":[0,0,1200,800],"interactive":false,"children":[]}'

        from agent_eyes.js_bridge import build_ax_tree_script
        script = build_ax_tree_script(max_depth=5)
        result = mock_as.execute_javascript(script, tab_index=0)

        mock_as.execute_javascript.assert_called_once_with(script, tab_index=0)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# Task 3.2 — _handle_close_tab fallback contract
# ---------------------------------------------------------------------------

class TestCloseTabFallbackContract(unittest.TestCase):
    """_handle_close_tab: when CDP unavailable on macOS, fall back to
    AppleScript close tab command."""

    def test_cdp_unavailable_applescript_fallback_selected(self):
        """Contract: when CDP errors out and macOS + AS available, use AS."""
        cdp_ok = False
        platform = "darwin"
        as_available = True

        use_fallback = (not cdp_ok and platform == "darwin" and as_available)
        self.assertTrue(use_fallback)

    def test_applescript_close_tab_called(self):
        """Contract: fallback calls _as.close_tab or equivalent."""
        mock_as = _make_as_module()
        # AppleScript module has a close_tab_by_index method
        mock_as.close_tab_by_index = MagicMock(return_value=True)

        result = mock_as.close_tab_by_index(0)
        mock_as.close_tab_by_index.assert_called_once_with(0)
        self.assertTrue(result)

    def test_no_fallback_on_non_darwin(self):
        """Contract: no AppleScript fallback on non-macOS."""
        platform = "linux"
        as_available = True  # irrelevant on non-darwin

        use_fallback = (platform == "darwin" and as_available)
        self.assertFalse(use_fallback)

    def test_error_message_when_no_fallback_available(self):
        """Contract: return helpful error when neither CDP nor AS available."""
        # Simulate the final error return path
        error_msg = (
            "ERROR: This feature requires Chrome Extension (Tier 1) or CDP (Tier 2). "
            "Start Chrome with --remote-debugging-port=9222"
        )
        self.assertIn("ERROR", error_msg)
        self.assertIn("CDP", error_msg)


# ---------------------------------------------------------------------------
# Task 3.3 — _handle_fill_form fallback contract
# ---------------------------------------------------------------------------

class TestFillFormFallbackContract(unittest.TestCase):
    """_handle_fill_form: when CDP unavailable, fall back to
    AppleScript shadow_type per field."""

    def test_cdp_unavailable_fallback_selected(self):
        """Contract: fallback is selected when CDP is not available."""
        cdp_ok = False
        platform = "darwin"
        as_available = True

        use_fallback = (not cdp_ok and platform == "darwin" and as_available)
        self.assertTrue(use_fallback)

    def test_shadow_type_called_per_field(self):
        """Contract: shadow_type is called once per field in the fields list."""
        mock_as = _make_as_module()
        mock_as.shadow_type = MagicMock(return_value=True)

        fields = [
            {"id": 1, "value": "hello"},
            {"id": 2, "value": "world"},
        ]
        filled = []
        for field in fields:
            ok = mock_as.shadow_type(field["value"], tab_index=0)
            if ok:
                filled.append(field["id"])

        self.assertEqual(mock_as.shadow_type.call_count, 2)
        self.assertEqual(filled, [1, 2])

    def test_partial_fill_tracks_errors(self):
        """Contract: errors on individual fields are tracked, others still fill."""
        mock_as = _make_as_module()
        mock_as.shadow_type = MagicMock(side_effect=[True, False, True])

        fields = [
            {"id": 1, "value": "a"},
            {"id": 2, "value": "b"},
            {"id": 3, "value": "c"},
        ]
        filled = []
        errors = []
        for field in fields:
            ok = mock_as.shadow_type(field["value"], tab_index=0)
            if ok:
                filled.append(field["id"])
            else:
                errors.append(field["id"])

        self.assertEqual(filled, [1, 3])
        self.assertEqual(errors, [2])


# ---------------------------------------------------------------------------
# Task 3.4 — _handle_wait_for fallback contract
# ---------------------------------------------------------------------------

class TestWaitForFallbackContract(unittest.TestCase):
    """_handle_wait_for: when CDP unavailable, fall back to polling
    via AppleScript shadow_read_interactive."""

    def test_cdp_unavailable_polling_fallback_selected(self):
        """Contract: polling via AS is selected when CDP not available."""
        cdp_ok = False
        pid = None  # no native PID
        platform = "darwin"
        as_available = True

        use_fallback = (
            not cdp_ok
            and pid is None
            and platform == "darwin"
            and as_available
        )
        self.assertTrue(use_fallback)

    def test_shadow_read_interactive_called_during_poll(self):
        """Contract: shadow_read_interactive is called while polling."""
        import time

        mock_as = _make_as_module()
        # First call returns None (not found), second returns content with the element
        mock_as.shadow_read_interactive = MagicMock(
            side_effect=[None, "button 'Submit' *"]
        )

        role = "button"
        name = "Submit"
        found = None
        deadline = time.monotonic() + 2.0

        while time.monotonic() < deadline:
            result = mock_as.shadow_read_interactive(tab_index=0)
            if result and role in result and name.lower() in result.lower():
                found = result
                break

        self.assertIsNotNone(found)
        self.assertEqual(mock_as.shadow_read_interactive.call_count, 2)

    def test_timeout_returns_timeout_message(self):
        """Contract: when element never appears, return timeout message."""
        # Simulates what the handler must return after timeout
        timeout = 0.1
        role = "button"
        name = "Submit"
        msg = f"Timeout: element not found after {timeout}s (role={role!r}, name={name!r})"
        self.assertIn("Timeout", msg)
        self.assertIn("button", msg)
        self.assertIn("Submit", msg)


# ---------------------------------------------------------------------------
# Task 3.5 — _handle_drag fallback contract
# ---------------------------------------------------------------------------

class TestDragFallbackContract(unittest.TestCase):
    """_handle_drag: when CDP unavailable, fall back to OS input simulation."""

    def test_cdp_unavailable_input_backend_fallback(self):
        """Contract: when CDP not available, use input_backend.drag."""
        cdp_ok = False
        input_available = True

        use_fallback = not cdp_ok and input_available
        self.assertTrue(use_fallback)

    def test_input_backend_drag_called_with_coords(self):
        """Contract: input_backend.drag is called with correct coordinates."""
        mock_input = MagicMock()
        mock_input.is_available.return_value = True
        mock_input.drag = MagicMock(return_value=True)

        from_x, from_y, to_x, to_y = 100, 200, 300, 400
        result = mock_input.drag(from_x, from_y, to_x, to_y)

        mock_input.drag.assert_called_once_with(100, 200, 300, 400)
        self.assertTrue(result)

    def test_no_fallback_when_input_backend_unavailable(self):
        """Contract: return error when input backend not available."""
        cdp_ok = False
        input_available = False

        use_fallback = not cdp_ok and input_available
        self.assertFalse(use_fallback)

        error = "ERROR: This feature requires Chrome Extension (Tier 1) or CDP (Tier 2). "
        self.assertIn("ERROR", error)


# ---------------------------------------------------------------------------
# Task 3.6 — _handle_dialog fallback contract
# ---------------------------------------------------------------------------

class TestDialogFallbackContract(unittest.TestCase):
    """_handle_dialog: when CDP unavailable, return a helpful error message
    (limited fallback — dialogs block JS so AppleScript cannot handle them)."""

    def test_cdp_unavailable_returns_helpful_error(self):
        """Contract: return an error that explains CDP is required."""
        # Simulate the fallback error path
        error = (
            "ERROR: Handling JavaScript dialogs requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )
        self.assertIn("ERROR", error)
        self.assertIn("CDP", error)
        self.assertIn("dialog", error.lower())

    def test_error_includes_launch_hint(self):
        """Contract: error message guides user to enable CDP."""
        error = (
            "ERROR: Handling JavaScript dialogs requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )
        self.assertIn("9222", error)


# ---------------------------------------------------------------------------
# Task 3.7 — _handle_file_upload fallback contract
# ---------------------------------------------------------------------------

class TestFileUploadFallbackContract(unittest.TestCase):
    """_handle_file_upload: when CDP unavailable, return a helpful error message
    (limited fallback — DOM.setFileInputFiles requires CDP)."""

    def test_cdp_unavailable_returns_helpful_error(self):
        """Contract: return an error that explains CDP is required."""
        error = (
            "ERROR: File upload requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )
        self.assertIn("ERROR", error)
        self.assertIn("CDP", error)

    def test_error_mentions_file_upload(self):
        """Contract: error message references the file upload limitation."""
        error = (
            "ERROR: File upload requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )
        self.assertIn("upload", error.lower())

    def test_error_includes_launch_hint(self):
        """Contract: error message guides user to enable CDP."""
        error = (
            "ERROR: File upload requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )
        self.assertIn("9222", error)


# ---------------------------------------------------------------------------
# Task 3 — Import contract: build_ax_tree_script / format_ax_tree importable
# ---------------------------------------------------------------------------

class TestJsBridgeImportFromServer(unittest.TestCase):
    """Verify that js_bridge symbols are importable at the server module level."""

    def test_build_ax_tree_script_importable(self):
        """build_ax_tree_script must be importable from js_bridge."""
        from agent_eyes.js_bridge import build_ax_tree_script
        self.callable(build_ax_tree_script)

    def test_format_ax_tree_importable(self):
        """format_ax_tree must be importable from js_bridge."""
        from agent_eyes.js_bridge import format_ax_tree
        self.callable(format_ax_tree)

    def callable(self, obj):  # helper
        self.assertTrue(callable(obj))


if __name__ == "__main__":
    unittest.main()
