"""Windows accessibility adapter using UI Automation via comtypes COM interface."""
from __future__ import annotations

import sys
from .base import BaseAdapter, UIElement, AppInfo


# UI Automation control type IDs mapped to readable role names
_UIA_CONTROL_TYPE_NAMES = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "toolbar",
    50021: "tooltip",
    50022: "tree",
    50023: "treeitem",
    50024: "custom",
    50025: "group",
    50026: "thumb",
    50027: "datagrid",
    50028: "dataitem",
    50029: "document",
    50030: "splitbutton",
    50031: "window",
    50032: "pane",
    50033: "header",
    50034: "headeritem",
    50035: "table",
    50036: "titlebar",
    50037: "separator",
}

# UI Automation Pattern IDs
_UIA_INVOKE_PATTERN_ID = 10000
_UIA_VALUE_PATTERN_ID = 10002
_UIA_SELECTION_ITEM_PATTERN_ID = 10010
_UIA_TOGGLE_PATTERN_ID = 10015
_UIA_SCROLL_PATTERN_ID = 10004
_UIA_EXPAND_COLLAPSE_PATTERN_ID = 10005

# UIA property IDs
_UIA_PROCESS_ID_PROPERTY_ID = 30008
_UIA_CONTROL_TYPE_PROPERTY_ID = 30003
_UIA_NAME_PROPERTY_ID = 30005
_UIA_IS_ENABLED_PROPERTY_ID = 30010
_UIA_IS_OFFSCREEN_PROPERTY_ID = 30022
_UIA_HAS_KEYBOARD_FOCUS_PROPERTY_ID = 30008
_UIA_BOUNDING_RECTANGLE_PROPERTY_ID = 30001
_UIA_VALUE_VALUE_PROPERTY_ID = 30045

# TreeScope
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_DESCENDANTS = 4
_TREE_SCOPE_SUBTREE = 7


class WindowsAdapter(BaseAdapter):
    """Windows UI Automation via comtypes COM interface."""

    _MAX_ELEMENTS = 1000
    _WEB_MAX_ELEMENTS = 3000

    def __init__(self):
        self._uia = None
        self._root = None
        self._id_counter = 0
        self._in_web_area = False
        try:
            import comtypes
            import comtypes.client
            # CUIAutomation CLSID
            self._uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            )
            self._root = self._uia.GetRootElement()
        except (ImportError, OSError, Exception):
            pass

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0
        self._in_web_area = False

    def is_available(self) -> bool:
        return self._uia is not None and sys.platform == "win32"

    def check_permissions(self) -> tuple[bool, str]:
        return True, "UI Automation is system-level"

    def list_apps(self) -> list[AppInfo]:
        if self._uia is None or self._root is None:
            return []
        apps = []
        seen_pids: set[int] = set()
        try:
            condition = self._uia.CreateTrueCondition()
            children = self._root.FindAll(_TREE_SCOPE_CHILDREN, condition)
            count = children.Length
            for i in range(count):
                try:
                    el = children.GetElement(i)
                    pid = el.CurrentProcessId
                    if pid in seen_pids:
                        continue
                    seen_pids.add(pid)

                    name = el.CurrentName or ""
                    ctrl_type = el.CurrentControlType
                    role = _UIA_CONTROL_TYPE_NAMES.get(ctrl_type, "window")

                    # Collect all visible window titles for this PID
                    windows: list[str] = []
                    for j in range(count):
                        try:
                            w = children.GetElement(j)
                            if w.CurrentProcessId == pid:
                                t = w.CurrentName or ""
                                if t:
                                    windows.append(t)
                        except Exception:
                            continue

                    apps.append(AppInfo(
                        pid=pid,
                        name=name if name else role,
                        windows=windows,
                    ))
                except Exception:
                    continue
        except Exception:
            pass
        return apps

    # Roles worth exposing inside web content
    _WEB_INTERACTIVE_ROLES = frozenset({
        "button", "hyperlink", "edit", "combobox", "checkbox",
        "radiobutton", "slider", "menuitem", "tabitem", "hyperlink",
        "listitem", "dataitem", "treeitem",
    })
    _WEB_STRUCTURAL_ROLES = frozenset({
        "heading", "list", "table", "dataitem", "header",
        "document", "group", "pane", "toolbar", "menu",
        "tab", "statusbar",
    })

    def _should_include_web_element(self, role: str, name: str) -> bool:
        if role in self._WEB_INTERACTIVE_ROLES:
            return True
        if role in self._WEB_STRUCTURAL_ROLES and name:
            return True
        return False

    def _element_to_ui(self, el, depth: int, max_depth: int,
                       in_web_area: bool = False) -> UIElement | None:
        if depth > max_depth:
            return None
        cap = self._WEB_MAX_ELEMENTS if self._in_web_area else self._MAX_ELEMENTS
        if self._id_counter >= cap:
            return None

        try:
            ctrl_type = el.CurrentControlType
            role = _UIA_CONTROL_TYPE_NAMES.get(ctrl_type, "unknown")
            name = el.CurrentName or ""

            # Detect entry into web content (document role)
            if role == "document":
                in_web_area = True
                self._in_web_area = True
                max_depth = max(max_depth, depth + 20)

            # Get value via ValuePattern if available
            value = ""
            try:
                value_pattern = el.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
                if value_pattern:
                    value = value_pattern.CurrentValue or ""
            except Exception:
                pass

            # States
            states: list[str] = []
            try:
                if el.CurrentIsEnabled:
                    states.append("enabled")
                else:
                    states.append("disabled")
                if not el.CurrentIsOffscreen:
                    states.append("visible")
                if el.CurrentHasKeyboardFocus:
                    states.append("focused")
            except Exception:
                pass

            # Bounds
            bounds = None
            try:
                rect = el.CurrentBoundingRectangle
                # rect is a RECT structure with left, top, right, bottom
                left = rect.left
                top = rect.top
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width > 0 and height > 0:
                    bounds = (left, top, width, height)
            except Exception:
                pass

            element = UIElement(
                id=self._next_id(),
                role=role,
                name=name,
                value=value,
                states=states,
                bounds=bounds,
                platform_ref=el,
                source="native",
            )

            # Web filtering: inside document, only keep interactive/structural
            if in_web_area and role != "document":
                if not self._should_include_web_element(role, name):
                    if depth < max_depth:
                        try:
                            condition = self._uia.CreateTrueCondition()
                            children = el.FindAll(_TREE_SCOPE_CHILDREN, condition)
                            for i in range(children.Length):
                                child = children.GetElement(i)
                                child_ui = self._element_to_ui(
                                    child, depth + 1, max_depth, in_web_area=True,
                                )
                                if child_ui:
                                    element.children.append(child_ui)
                        except Exception:
                            pass
                    if element.children:
                        return element
                    return None

            if depth < max_depth:
                try:
                    condition = self._uia.CreateTrueCondition()
                    children = el.FindAll(_TREE_SCOPE_CHILDREN, condition)
                    for i in range(children.Length):
                        child = children.GetElement(i)
                        child_ui = self._element_to_ui(
                            child, depth + 1, max_depth, in_web_area=in_web_area,
                        )
                        if child_ui:
                            element.children.append(child_ui)
                except Exception:
                    pass

            return element
        except Exception:
            return None

    def get_tree(self, pid: int, max_depth: int = 5,
                 is_browser: bool = False) -> UIElement | None:
        if self._uia is None or self._root is None:
            return None
        self.reset_ids()
        try:
            condition = self._uia.CreatePropertyCondition(
                _UIA_PROCESS_ID_PROPERTY_ID, pid,
            )
            el = self._root.FindFirst(_TREE_SCOPE_DESCENDANTS, condition)
            if el is None:
                return None
            return self._element_to_ui(el, 0, max_depth)
        except Exception:
            return None

    def find_elements(
        self, pid: int, role: str = "", name: str = "", value: str = ""
    ) -> list[UIElement]:
        tree = self.get_tree(pid, max_depth=8)
        if tree is None:
            return []
        results: list[UIElement] = []
        self._search(tree, role.lower(), name.lower(), value.lower(), results)
        return results

    def _search(self, el: UIElement, role: str, name: str, value: str,
                results: list) -> None:
        match = True
        if role and role not in el.role:
            match = False
        if name and name not in el.name.lower():
            match = False
        if value and value not in el.value.lower():
            match = False
        if match and (role or name or value):
            results.append(el)
        for child in el.children:
            self._search(child, role, name, value, results)

    def perform_action(self, element: UIElement, action: str) -> bool:
        if element.platform_ref is None:
            return False
        el = element.platform_ref
        try:
            if action in ("press", "click", "invoke"):
                pattern = el.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
                pattern.Invoke()
            elif action == "toggle":
                pattern = el.GetCurrentPattern(_UIA_TOGGLE_PATTERN_ID)
                pattern.Toggle()
            elif action == "select":
                pattern = el.GetCurrentPattern(_UIA_SELECTION_ITEM_PATTERN_ID)
                pattern.Select()
            elif action == "expand":
                pattern = el.GetCurrentPattern(_UIA_EXPAND_COLLAPSE_PATTERN_ID)
                pattern.Expand()
            elif action == "collapse":
                pattern = el.GetCurrentPattern(_UIA_EXPAND_COLLAPSE_PATTERN_ID)
                pattern.Collapse()
            else:
                # Default: try invoke
                pattern = el.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
                pattern.Invoke()
            return True
        except Exception:
            return False

    def focus_element(self, element: UIElement) -> bool:
        if element.platform_ref is None:
            return False
        try:
            element.platform_ref.SetFocus()
            return True
        except Exception:
            return False

    def set_value(self, element: UIElement, value: str) -> bool:
        if element.platform_ref is None:
            return False
        el = element.platform_ref
        try:
            pattern = el.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
            pattern.SetValue(value)
            return True
        except Exception:
            return False

    def get_focused_element(self) -> UIElement | None:
        if self._uia is None:
            return None
        self.reset_ids()
        try:
            el = self._uia.GetFocusedElement()
            if el is None:
                return None
            return self._element_to_ui(el, 0, 2)
        except Exception:
            return None
