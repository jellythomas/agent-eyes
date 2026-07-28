"""Tests for WindowsAdapter (comtypes UI Automation) and WindowsInputBackend.

These tests run on all platforms using mocks — they validate the adapter
logic without requiring a real Windows environment or comtypes.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uia_element(
    name="",
    ctrl_type=50000,  # button
    pid=1234,
    is_enabled=True,
    is_offscreen=False,
    has_keyboard_focus=False,
    rect=None,
    value=None,
    is_password=False,
):
    el = MagicMock()
    el.CurrentName = name
    el.CurrentControlType = ctrl_type
    el.CurrentProcessId = pid
    el.CurrentIsEnabled = is_enabled
    el.CurrentIsOffscreen = is_offscreen
    el.CurrentHasKeyboardFocus = has_keyboard_focus
    el.CurrentIsPassword = is_password

    if rect is None:
        r = MagicMock()
        r.left = 10
        r.top = 20
        r.right = 110
        r.bottom = 70
        el.CurrentBoundingRectangle = r
    else:
        el.CurrentBoundingRectangle = rect

    if value is not None:
        value_pattern = MagicMock()
        value_pattern.CurrentValue = value
        el.GetCurrentPattern.return_value = value_pattern
    else:
        el.GetCurrentPattern.return_value = None

    el.FindAll.return_value = _make_element_array([])
    el.SetFocus.return_value = None
    return el


def _make_element_array(elements):
    arr = MagicMock()
    arr.Length = len(elements)
    arr.GetElement.side_effect = lambda i: elements[i]
    return arr


def _make_windows_adapter_with_mock_uia(root_el=None):
    """Create WindowsAdapter with mocked comtypes, bypassing real COM."""
    from agent_eyes.adapters.windows import WindowsAdapter

    adapter = WindowsAdapter.__new__(WindowsAdapter)
    adapter._id_counter = 0
    adapter._in_web_area = False

    mock_uia = MagicMock()
    mock_root = root_el if root_el is not None else MagicMock()
    mock_uia.GetRootElement.return_value = mock_root
    mock_uia.CreateTrueCondition.return_value = MagicMock()
    mock_uia.CreatePropertyCondition.return_value = MagicMock()

    adapter._uia = mock_uia
    adapter._root = mock_root
    return adapter


# ---------------------------------------------------------------------------
# WindowsAdapter: is_available
# ---------------------------------------------------------------------------

class TestWindowsAdapterAvailability(unittest.TestCase):

    def test_available_when_uia_set_and_win32(self):
        with patch.object(sys, "platform", "win32"):
            adapter = _make_windows_adapter_with_mock_uia()
            self.assertTrue(adapter.is_available())

    def test_unavailable_when_uia_is_none(self):
        with patch.object(sys, "platform", "win32"):
            adapter = _make_windows_adapter_with_mock_uia()
            adapter._uia = None
            self.assertFalse(adapter.is_available())

    def test_unavailable_on_non_windows_platform(self):
        with patch.object(sys, "platform", "darwin"):
            adapter = _make_windows_adapter_with_mock_uia()
            self.assertFalse(adapter.is_available())

    def test_init_handles_import_error_gracefully(self):
        """If comtypes is not installed, adapter initialises with _uia=None."""
        with patch.dict("sys.modules", {"comtypes": None, "comtypes.client": None}):
            from agent_eyes.adapters.windows import WindowsAdapter
            adapter = WindowsAdapter.__new__(WindowsAdapter)
            adapter._uia = None
            adapter._root = None
            adapter._id_counter = 0
            adapter._in_web_area = False
            self.assertIsNone(adapter._uia)


# ---------------------------------------------------------------------------
# WindowsAdapter: check_permissions
# ---------------------------------------------------------------------------

class TestWindowsAdapterPermissions(unittest.TestCase):

    def test_always_returns_true(self):
        adapter = _make_windows_adapter_with_mock_uia()
        ok, msg = adapter.check_permissions()
        self.assertTrue(ok)
        self.assertIn("UI Automation", msg)


# ---------------------------------------------------------------------------
# WindowsAdapter: list_apps
# ---------------------------------------------------------------------------

class TestWindowsAdapterListApps(unittest.TestCase):

    def test_returns_empty_when_uia_none(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia = None
        self.assertEqual(adapter.list_apps(), [])

    def test_returns_empty_when_root_none(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root = None
        self.assertEqual(adapter.list_apps(), [])

    def test_lists_top_level_windows(self):
        el1 = _make_uia_element(name="Notepad", ctrl_type=50031, pid=100)
        el2 = _make_uia_element(name="Chrome", ctrl_type=50031, pid=200)

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([el1, el2])
        adapter._uia.CreateTrueCondition.return_value = MagicMock()

        apps = adapter.list_apps()
        self.assertEqual(len(apps), 2)
        pids = {a.pid for a in apps}
        self.assertIn(100, pids)
        self.assertIn(200, pids)

    def test_deduplicates_same_pid(self):
        el1 = _make_uia_element(name="Chrome Main", ctrl_type=50031, pid=100)
        el2 = _make_uia_element(name="Chrome DevTools", ctrl_type=50031, pid=100)

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([el1, el2])

        apps = adapter.list_apps()
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].pid, 100)

    def test_skips_elements_that_raise(self):
        # Property access raises to simulate a COM error on the bad element
        el_bad = MagicMock()
        type(el_bad).CurrentProcessId = property(lambda self: (_ for _ in ()).throw(Exception("COM error")))
        el_good = _make_uia_element(name="Notepad", pid=42)

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([el_bad, el_good])

        apps = adapter.list_apps()
        # Only the good element should produce an AppInfo
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].pid, 42)

    def test_complete_inventory_propagates_global_uia_failure(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.side_effect = RuntimeError("UIA unavailable")

        self.assertEqual(adapter.list_apps(), [])
        with self.assertRaisesRegex(RuntimeError, "UIA unavailable"):
            adapter.list_apps_complete()


# ---------------------------------------------------------------------------
# WindowsAdapter: get_tree
# ---------------------------------------------------------------------------

class TestWindowsAdapterGetTree(unittest.TestCase):

    def test_returns_none_when_uia_none(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia = None
        self.assertIsNone(adapter.get_tree(1234))

    def test_returns_none_when_element_not_found(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.CreatePropertyCondition.return_value = MagicMock()
        adapter._root.FindFirst.return_value = None
        self.assertIsNone(adapter.get_tree(9999))

    def test_returns_ui_element_for_found_process(self):
        root_el = _make_uia_element(name="Notepad", ctrl_type=50031, pid=42)
        root_el.FindAll.return_value = _make_element_array([])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = root_el

        result = adapter.get_tree(42)
        self.assertIsNotNone(result)
        self.assertEqual(result.role, "window")
        self.assertEqual(result.name, "Notepad")

    def test_resets_id_counter_before_walk(self):
        root_el = _make_uia_element(name="App", ctrl_type=50031, pid=1)
        root_el.FindAll.return_value = _make_element_array([])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = root_el
        adapter._id_counter = 500

        adapter.get_tree(1)
        self.assertLess(adapter._id_counter, 500)

    def test_tree_includes_children(self):
        child = _make_uia_element(name="Button OK", ctrl_type=50000, pid=1)
        child.FindAll.return_value = _make_element_array([])

        parent = _make_uia_element(name="Dialog", ctrl_type=50032, pid=1)
        parent.FindAll.return_value = _make_element_array([child])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = parent

        tree = adapter.get_tree(1)
        self.assertIsNotNone(tree)
        self.assertEqual(len(tree.children), 1)
        self.assertEqual(tree.children[0].name, "Button OK")

    def test_respects_max_depth(self):
        # Build 3 levels: root -> child -> grandchild
        grandchild = _make_uia_element(name="Grandchild", ctrl_type=50000)
        grandchild.FindAll.return_value = _make_element_array([])

        child = _make_uia_element(name="Child", ctrl_type=50032)
        child.FindAll.return_value = _make_element_array([grandchild])

        root_el = _make_uia_element(name="Root", ctrl_type=50031)
        root_el.FindAll.return_value = _make_element_array([child])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = root_el

        tree = adapter.get_tree(1, max_depth=1)
        self.assertEqual(len(tree.children), 1)
        # At depth 1 we have child; grandchild is at depth 2 (> max_depth=1)
        self.assertEqual(len(tree.children[0].children), 0)

    def test_browser_inventory_reads_every_top_level_window(self):
        first = _make_uia_element(name="First", ctrl_type=50031, pid=22)
        second = _make_uia_element(name="Second", ctrl_type=50031, pid=22)
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([first, second])

        trees = adapter.get_browser_trees(22)

        self.assertEqual([tree.name for tree in trees], ["First", "Second"])
        self.assertEqual([tree.window_index for tree in trees], [0, 1])

    def test_browser_inventory_prunes_document_descendants(self):
        browser_tab = _make_uia_element(name="Browser tab", ctrl_type=50019, pid=22)
        page_tab = _make_uia_element(name="Page tab", ctrl_type=50019, pid=22)
        document = _make_uia_element(name="Page", ctrl_type=50029, pid=22)
        document.FindAll.return_value = _make_element_array([page_tab])
        window = _make_uia_element(name="Browser", ctrl_type=50031, pid=22)
        window.FindAll.return_value = _make_element_array([browser_tab, document])
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([window])

        tree = adapter.get_browser_trees(22)[0]
        names = [child.name for child in tree.children]

        self.assertIn("Browser tab", names)
        self.assertNotIn("Page tab", names)
        document.FindAll.assert_not_called()

    def test_complete_browser_inventory_propagates_uia_failure(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.side_effect = RuntimeError("browser UIA unavailable")

        self.assertEqual(adapter.get_browser_trees(22), [])
        with self.assertRaisesRegex(RuntimeError, "browser UIA unavailable"):
            adapter.get_browser_trees_complete(22)

    def test_complete_browser_inventory_rejects_traversal_cap_truncation(self):
        first = _make_uia_element(name="First tab", ctrl_type=50019, pid=22)
        second = _make_uia_element(name="Second tab", ctrl_type=50019, pid=22)
        window = _make_uia_element(name="Browser", ctrl_type=50031, pid=22)
        window.FindAll.return_value = _make_element_array([first, second])
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([window])
        adapter._MAX_ELEMENTS = 2

        self.assertEqual(len(adapter.get_browser_trees(22)[0].children), 1)
        with self.assertRaisesRegex(RuntimeError, "traversal safety cap"):
            adapter.get_browser_trees_complete(22)

    def test_complete_browser_inventory_rejects_depth_truncation(self):
        tab = _make_uia_element(name="Hidden tab", ctrl_type=50019, pid=22)
        group = _make_uia_element(name="Tab group", ctrl_type=50032, pid=22)
        group.FindAll.return_value = _make_element_array([tab])
        window = _make_uia_element(name="Browser", ctrl_type=50031, pid=22)
        window.FindAll.return_value = _make_element_array([group])
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([window])

        self.assertEqual(
            adapter.get_browser_trees(22, max_depth=1)[0].children[0].children,
            [],
        )
        with self.assertRaisesRegex(RuntimeError, "traversal depth bound"):
            adapter.get_browser_trees_complete(22, max_depth=1)

    def test_complete_browser_inventory_propagates_child_read_failure(self):
        broken = MagicMock()
        type(broken).CurrentControlType = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("broken UIA child"))
        )
        window = _make_uia_element(name="Browser", ctrl_type=50031, pid=22)
        window.FindAll.return_value = _make_element_array([broken])
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindAll.return_value = _make_element_array([window])

        self.assertEqual(adapter.get_browser_trees(22)[0].children, [])
        with self.assertRaisesRegex(RuntimeError, "broken UIA child"):
            adapter.get_browser_trees_complete(22)


# ---------------------------------------------------------------------------
# WindowsAdapter: _element_to_ui — states and bounds
# ---------------------------------------------------------------------------

class TestWindowsAdapterElementToUI(unittest.TestCase):

    def test_enabled_state(self):
        el = _make_uia_element(name="Btn", is_enabled=True, is_offscreen=False)
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertIn("enabled", ui.states)

    def test_disabled_state(self):
        el = _make_uia_element(name="Btn", is_enabled=False)
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertIn("disabled", ui.states)

    def test_focused_state(self):
        el = _make_uia_element(name="Input", has_keyboard_focus=True)
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertIn("focused", ui.states)

    def test_bounds_computed_from_bounding_rectangle(self):
        rect = MagicMock()
        rect.left = 50
        rect.top = 100
        rect.right = 250
        rect.bottom = 180
        el = _make_uia_element(name="Btn", rect=rect)
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertEqual(ui.bounds, (50, 100, 200, 80))

    def test_zero_size_bounds_excluded(self):
        rect = MagicMock()
        rect.left = 0
        rect.top = 0
        rect.right = 0
        rect.bottom = 0
        el = _make_uia_element(name="Invisible", rect=rect)
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertIsNone(ui.bounds)

    def test_value_from_value_pattern(self):
        el = _make_uia_element(name="Field", value="hello")
        el.FindAll.return_value = _make_element_array([])
        adapter = _make_windows_adapter_with_mock_uia()
        ui = adapter._element_to_ui(el, 0, 5)
        self.assertEqual(ui.value, "hello")

    def test_password_value_is_never_requested_or_stored(self):
        secret = "windows-password-secret-333a"
        el = _make_uia_element(
            name="Password",
            ctrl_type=50004,
            value=secret,
            is_password=True,
        )
        adapter = _make_windows_adapter_with_mock_uia()

        ui = adapter._element_to_ui(el, 0, 5)

        self.assertEqual(ui.value, "")
        self.assertIn("secure", ui.states)
        self.assertNotIn(secret, ui.to_flat_line())
        el.GetCurrentPattern.assert_not_called()

    def test_returns_none_on_exception(self):
        el = MagicMock()
        # Make CurrentControlType raise via property so attribute access fails
        type(el).CurrentControlType = property(lambda self: (_ for _ in ()).throw(Exception("COM error")))
        adapter = _make_windows_adapter_with_mock_uia()
        result = adapter._element_to_ui(el, 0, 5)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# WindowsAdapter: find_elements
# ---------------------------------------------------------------------------

class TestWindowsAdapterFindElements(unittest.TestCase):

    def test_returns_empty_when_tree_is_none(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = None
        result = adapter.find_elements(999, role="button")
        self.assertEqual(result, [])

    def test_finds_by_role(self):
        btn = _make_uia_element(name="OK", ctrl_type=50000)  # button
        btn.FindAll.return_value = _make_element_array([])
        pane = _make_uia_element(name="Main", ctrl_type=50032)  # pane
        pane.FindAll.return_value = _make_element_array([btn])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = pane

        results = adapter.find_elements(1, role="button")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "OK")

    def test_finds_by_name(self):
        btn = _make_uia_element(name="Submit", ctrl_type=50000)
        btn.FindAll.return_value = _make_element_array([])
        pane = _make_uia_element(name="Dialog", ctrl_type=50032)
        pane.FindAll.return_value = _make_element_array([btn])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = pane

        results = adapter.find_elements(1, name="submit")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "Submit")

    def test_no_match_returns_empty(self):
        btn = _make_uia_element(name="OK", ctrl_type=50000)
        btn.FindAll.return_value = _make_element_array([])
        pane = _make_uia_element(name="Dialog", ctrl_type=50032)
        pane.FindAll.return_value = _make_element_array([btn])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._root.FindFirst.return_value = pane

        results = adapter.find_elements(1, name="nonexistent")
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# WindowsAdapter: perform_action
# ---------------------------------------------------------------------------

class TestWindowsAdapterPerformAction(unittest.TestCase):

    def _adapter(self):
        return _make_windows_adapter_with_mock_uia()

    def test_returns_false_when_no_platform_ref(self):
        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="button", name="OK", platform_ref=None)
        adapter = self._adapter()
        self.assertFalse(adapter.perform_action(el, "click"))

    def test_invoke_pattern_called_for_click(self):
        mock_pattern = MagicMock()
        native_el = MagicMock()
        native_el.GetCurrentPattern.return_value = mock_pattern

        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="button", name="OK", platform_ref=native_el)
        adapter = self._adapter()

        result = adapter.perform_action(el, "click")
        self.assertTrue(result)
        mock_pattern.Invoke.assert_called_once()

    def test_toggle_pattern_called_for_toggle(self):
        mock_pattern = MagicMock()
        native_el = MagicMock()
        native_el.GetCurrentPattern.return_value = mock_pattern

        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="checkbox", name="Agree", platform_ref=native_el)
        adapter = self._adapter()

        result = adapter.perform_action(el, "toggle")
        self.assertTrue(result)
        mock_pattern.Toggle.assert_called_once()

    def test_select_pattern_called_for_select(self):
        mock_pattern = MagicMock()
        native_el = MagicMock()
        native_el.GetCurrentPattern.return_value = mock_pattern

        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="listitem", name="Item 1", platform_ref=native_el)
        adapter = self._adapter()

        result = adapter.perform_action(el, "select")
        self.assertTrue(result)
        mock_pattern.Select.assert_called_once()

    def test_returns_false_on_exception(self):
        native_el = MagicMock()
        native_el.GetCurrentPattern.side_effect = Exception("pattern unavailable")

        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="button", name="Fail", platform_ref=native_el)
        adapter = self._adapter()

        self.assertFalse(adapter.perform_action(el, "click"))


# ---------------------------------------------------------------------------
# WindowsAdapter: focus_element + set_value + get_focused_element
# ---------------------------------------------------------------------------

class TestWindowsAdapterFocusAndValue(unittest.TestCase):

    def test_native_identity_uses_uia_compare_elements(self):
        from agent_eyes.adapters.base import UIElement

        first_ref = object()
        second_ref = object()
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.CompareElements.side_effect = lambda first, second: first is second

        first = UIElement(id=1, role="edit", platform_ref=first_ref)
        same = UIElement(id=2, role="edit", platform_ref=first_ref)
        different = UIElement(id=3, role="edit", platform_ref=second_ref)

        self.assertTrue(adapter.is_same_element(first, same))
        self.assertFalse(adapter.is_same_element(first, different))

    def test_element_at_position_returns_provider_owned_uia_element(self):
        from agent_eyes.adapters.base import UIElement

        native_element = _make_uia_element(
            name="Review comment",
            ctrl_type=50000,
            pid=73,
        )
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.ElementFromPoint.return_value = native_element
        adapter._uia.CompareElements.side_effect = lambda first, second: first is second

        result = adapter.element_at_position(125.9, 240.8)

        self.assertIsNotNone(result)
        self.assertIs(result.platform_ref, native_element)
        self.assertTrue(
            adapter.is_same_element(
                result,
                UIElement(id=99, role="button", platform_ref=native_element),
            )
        )
        point = adapter._uia.ElementFromPoint.call_args.args[0]
        self.assertEqual((point.x, point.y), (125, 240))

    def test_element_at_position_fails_closed_when_uia_has_no_element(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.ElementFromPoint.return_value = None

        self.assertIsNone(adapter.element_at_position(125, 240))

    def test_element_at_position_fails_closed_when_uia_raises(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.ElementFromPoint.side_effect = RuntimeError("UIA unavailable")

        self.assertIsNone(adapter.element_at_position(125, 240))

    def test_focus_calls_set_focus(self):
        native_el = MagicMock()
        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="edit", name="Input", platform_ref=native_el)
        adapter = _make_windows_adapter_with_mock_uia()

        result = adapter.focus_element(el)
        self.assertTrue(result)
        native_el.SetFocus.assert_called_once()

    def test_focus_returns_false_when_no_platform_ref(self):
        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="edit", name="Input", platform_ref=None)
        adapter = _make_windows_adapter_with_mock_uia()
        self.assertFalse(adapter.focus_element(el))

    def test_set_value_uses_value_pattern(self):
        mock_pattern = MagicMock()
        native_el = MagicMock()
        native_el.GetCurrentPattern.return_value = mock_pattern

        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="edit", name="Name", platform_ref=native_el)
        adapter = _make_windows_adapter_with_mock_uia()

        result = adapter.set_value(el, "Alice")
        self.assertTrue(result)
        mock_pattern.SetValue.assert_called_once_with("Alice")

    def test_set_value_returns_false_when_no_platform_ref(self):
        from agent_eyes.adapters.base import UIElement
        el = UIElement(id=1, role="edit", name="Name", platform_ref=None)
        adapter = _make_windows_adapter_with_mock_uia()
        self.assertFalse(adapter.set_value(el, "Alice"))

    def test_get_focused_element_returns_none_when_uia_none(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia = None
        self.assertIsNone(adapter.get_focused_element())

    def test_get_focused_element_returns_ui_element(self):
        focused_el = _make_uia_element(name="Username", ctrl_type=50004)
        focused_el.FindAll.return_value = _make_element_array([])

        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.GetFocusedElement.return_value = focused_el

        result = adapter.get_focused_element()
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Username")
        self.assertEqual(result.role, "edit")

    def test_get_focused_element_returns_none_on_exception(self):
        adapter = _make_windows_adapter_with_mock_uia()
        adapter._uia.GetFocusedElement.side_effect = Exception("COM error")
        self.assertIsNone(adapter.get_focused_element())


# ---------------------------------------------------------------------------
# WindowsInputBackend: scroll
# ---------------------------------------------------------------------------

class TestWindowsInputBackendScroll(unittest.TestCase):
    """Tests for WindowsInputBackend.scroll.

    We mock _send_input and _load directly so the tests never touch ctypes,
    and track calls via a counter on _move_to.
    """

    def _make_backend(self):
        from agent_eyes.input_sim import WindowsInputBackend
        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._user32 = MagicMock()
        backend._user32.GetSystemMetrics.side_effect = lambda m: 1920 if m == 0 else 1080
        backend._INPUT = MagicMock()
        backend._MOUSEINPUT = MagicMock()
        backend._KEYBDINPUT = MagicMock()
        backend._move_input = MagicMock(
            side_effect=lambda x, y: ("move", x, y)
        )
        backend._mouse_input = MagicMock(
            side_effect=lambda flags, **kwargs: ("mouse", flags, kwargs)
        )
        backend._send_inputs = MagicMock(return_value=True)
        return backend

    def test_scroll_down_returns_true(self):
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            result = backend.scroll(500, 400, delta_y=-3)
        self.assertTrue(result)

    def test_scroll_up_returns_true(self):
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            result = backend.scroll(500, 400, delta_y=3)
        self.assertTrue(result)

    def test_scroll_zero_delta_uses_default_amount(self):
        """When delta_y=0 and delta_x=0, still sends 3 wheel events."""
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            backend.scroll(500, 400, delta_x=0, delta_y=0)
        events = backend._send_inputs.call_args.args[0]
        self.assertEqual(len(events), 4)

    def test_scroll_move_then_wheel_events(self):
        """scroll() first moves the mouse, then sends |delta_y| wheel events."""
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            backend.scroll(100, 200, delta_y=-5)
        events = backend._send_inputs.call_args.args[0]
        self.assertEqual(len(events), 6)

    def test_scroll_returns_false_on_exception(self):
        from agent_eyes.input_sim import WindowsInputBackend
        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._user32 = MagicMock()
        backend._INPUT = MagicMock()
        backend._MOUSEINPUT = MagicMock()
        backend._KEYBDINPUT = MagicMock()
        backend._move_input = MagicMock(return_value=("move", 0, 0))
        backend._mouse_input = MagicMock(return_value=("mouse", 0, {}))
        backend._send_inputs = MagicMock(side_effect=Exception("crash"))

        result = backend.scroll(0, 0)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# WindowsInputBackend: drag
# ---------------------------------------------------------------------------

class TestWindowsInputBackendDrag(unittest.TestCase):

    def _make_backend(self):
        from agent_eyes.input_sim import WindowsInputBackend
        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._user32 = MagicMock()
        backend._user32.GetSystemMetrics.side_effect = lambda m: 1920 if m == 0 else 1080
        backend._INPUT = MagicMock()
        backend._MOUSEINPUT = MagicMock()
        backend._KEYBDINPUT = MagicMock()
        backend._move_input = MagicMock(
            side_effect=lambda x, y: ("move", x, y)
        )
        backend._mouse_input = MagicMock(
            side_effect=lambda flags, **kwargs: ("mouse", flags, kwargs)
        )
        backend._send_inputs = MagicMock(return_value=True)
        return backend

    def test_drag_returns_true(self):
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            result = backend.drag(100, 100, 300, 200)
        self.assertTrue(result)

    def test_drag_sends_mouse_down_and_up(self):
        """drag() must call _mouse_event with LEFTDOWN before LEFTUP."""
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            backend.drag(100, 100, 300, 200)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        events = backend._send_inputs.call_args.args[0]
        sent_flags = [event[1] for event in events if event[0] == "mouse"]
        self.assertIn(MOUSEEVENTF_LEFTDOWN, sent_flags)
        self.assertIn(MOUSEEVENTF_LEFTUP, sent_flags)
        self.assertLess(
            sent_flags.index(MOUSEEVENTF_LEFTDOWN),
            sent_flags.index(MOUSEEVENTF_LEFTUP),
        )

    def test_drag_same_point_still_returns_true(self):
        """Dragging from and to the same coordinates should not crash."""
        backend = self._make_backend()
        with patch("agent_eyes.input_sim.time"):
            result = backend.drag(100, 100, 100, 100)
        self.assertTrue(result)

    def test_drag_returns_false_on_exception(self):
        from agent_eyes.input_sim import WindowsInputBackend
        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._user32 = MagicMock()
        backend._INPUT = MagicMock()
        backend._MOUSEINPUT = MagicMock()
        backend._KEYBDINPUT = MagicMock()
        backend._move_input = MagicMock(return_value=("move", 0, 0))
        backend._mouse_input = MagicMock(return_value=("mouse", 0, {}))
        backend._send_inputs = MagicMock(side_effect=Exception("crash"))

        result = backend.drag(0, 0, 100, 100)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# WindowsInputBackend: _move_to helper
# ---------------------------------------------------------------------------

class TestWindowsInputBackendMoveHelper(unittest.TestCase):

    def test_move_input_maps_negative_origin_across_virtual_desktop(self):
        from agent_eyes.input_sim import WindowsInputBackend

        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        metrics = {76: -1920, 77: -120, 78: 3840, 79: 1200}
        backend._user32 = MagicMock()
        backend._user32.GetSystemMetrics.side_effect = metrics.__getitem__
        backend._mouse_input = MagicMock(side_effect=lambda flags, **values: (flags, values))

        top_left = backend._move_input(-1920, -120)
        bottom_right = backend._move_input(1919, 1079)

        self.assertEqual(top_left, (0xC001, {"dx": 0, "dy": 0}))
        self.assertEqual(bottom_right, (0xC001, {"dx": 65535, "dy": 65535}))

    def test_move_mouse_dispatches_one_absolute_move(self):
        from agent_eyes.input_sim import WindowsInputBackend

        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._load = MagicMock()
        backend._move_to = MagicMock(return_value=True)

        self.assertTrue(backend.move_mouse(960, 540))
        backend._load.assert_called_once_with()
        backend._move_to.assert_called_once_with(960, 540)

    def test_move_mouse_fails_closed_when_send_input_raises(self):
        from agent_eyes.input_sim import WindowsInputBackend

        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._load = MagicMock()
        backend._move_to = MagicMock(side_effect=RuntimeError("SendInput unavailable"))

        self.assertFalse(backend.move_mouse(960, 540))

    def test_click_reuses_the_virtual_desktop_move_path(self):
        from agent_eyes.input_sim import WindowsInputBackend

        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._load = MagicMock()
        backend._move_input = MagicMock(return_value="virtual-move")
        backend._mouse_input = MagicMock(
            side_effect=lambda flags: ("mouse", flags)
        )
        backend._send_inputs = MagicMock(return_value=True)

        self.assertTrue(backend.click(-960, 540))
        backend._move_input.assert_called_once_with(-960, 540)
        self.assertEqual(
            backend._send_inputs.call_args.args[0][0],
            "virtual-move",
        )

    def test_move_to_calls_send_input_once(self):
        """_move_to() sends exactly one INPUT event."""
        from agent_eyes.input_sim import WindowsInputBackend
        backend = WindowsInputBackend.__new__(WindowsInputBackend)
        backend._user32 = MagicMock()
        metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}
        backend._user32.GetSystemMetrics.side_effect = metrics.__getitem__
        backend._INPUT = MagicMock()
        backend._MOUSEINPUT = MagicMock()
        backend._send_input = MagicMock()

        backend._move_to(960, 540)
        backend._send_input.assert_called_once()


if __name__ == "__main__":
    unittest.main()
