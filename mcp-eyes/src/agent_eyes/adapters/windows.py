"""Windows accessibility adapter using UI Automation via pywinauto."""
from __future__ import annotations

import sys
from .base import BaseAdapter, UIElement, AppInfo


class WindowsAdapter(BaseAdapter):
    """Windows UI Automation adapter using pywinauto."""

    _MAX_ELEMENTS = 1000

    def __init__(self):
        self._id_counter = 0

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import pywinauto  # noqa: F401
            return True
        except ImportError:
            return False

    def check_permissions(self) -> tuple[bool, str]:
        # Windows UIA doesn't require special permissions for most apps
        # UAC-elevated apps may need the script to run as admin
        return True, "Windows UI Automation available (no special permissions needed)."

    def list_apps(self) -> list[AppInfo]:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        apps = []
        seen_pids = set()

        for win in desktop.windows():
            try:
                pid = win.process_id()
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)

                title = win.window_text() or ""
                name = win.friendly_class_name() or ""

                # Collect all window titles for this PID
                windows = []
                for w2 in desktop.windows():
                    if w2.process_id() == pid:
                        t = w2.window_text()
                        if t:
                            windows.append(t)

                apps.append(AppInfo(
                    pid=pid,
                    name=name if name else title,
                    windows=windows,
                ))
            except Exception:
                continue

        return apps

    # Roles worth exposing inside web content (interactive + structural)
    _WEB_INTERACTIVE_ROLES = frozenset({
        "button", "hyperlink", "edit", "combobox", "checkbox",
        "radiobutton", "slider", "menuitem", "tabitem", "link",
        "listitem", "dataitem", "treeitem",
    })
    _WEB_STRUCTURAL_ROLES = frozenset({
        "heading", "list", "table", "dataitem", "header",
        "document", "group", "pane", "toolbar", "menu",
        "tabcontrol", "statusbar",
    })

    def _should_include_web_element(self, role: str, name: str) -> bool:
        """Filter web elements: keep interactive + named structural, skip noise."""
        if role in self._WEB_INTERACTIVE_ROLES:
            return True
        if role in self._WEB_STRUCTURAL_ROLES and name:
            return True
        return False

    def _wrapper_to_ui(self, wrapper, depth: int, max_depth: int,
                       in_web_area: bool = False) -> UIElement | None:
        if depth > max_depth:
            return None
        if self._id_counter >= self._MAX_ELEMENTS:
            return None

        try:
            role = wrapper.friendly_class_name() or "unknown"
            name = wrapper.window_text() or ""

            # Detect entry into web content
            if role.lower() == "document":
                in_web_area = True
                max_depth = max(max_depth, depth + 10)

            # Get automation element properties
            value = ""
            try:
                auto_el = wrapper.element_info
                value = str(getattr(auto_el, "value", "") or "")
            except Exception:
                pass

            states = []
            try:
                if wrapper.is_enabled():
                    states.append("enabled")
                else:
                    states.append("disabled")
                if wrapper.is_visible():
                    states.append("visible")
                if wrapper.has_keyboard_focus():
                    states.append("focused")
            except Exception:
                pass

            bounds = None
            try:
                rect = wrapper.rectangle()
                bounds = (rect.left, rect.top, rect.width(), rect.height())
            except Exception:
                pass

            element = UIElement(
                id=self._next_id(),
                role=role.lower(),
                name=name,
                value=value,
                states=states,
                bounds=bounds,
                platform_ref=wrapper,
                source="native",
            )

            # Web filtering: inside Document, only keep interactive/structural
            if in_web_area and role.lower() != "document":
                if not self._should_include_web_element(role.lower(), name):
                    if depth < max_depth:
                        try:
                            for child in wrapper.children():
                                child_ui = self._wrapper_to_ui(
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
                    for child in wrapper.children():
                        child_ui = self._wrapper_to_ui(
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
        from pywinauto import Application

        self.reset_ids()
        try:
            app = Application(backend="uia").connect(process=pid)
        except Exception:
            return None

        try:
            window = app.top_window()
        except Exception:
            return None

        return self._wrapper_to_ui(window.wrapper_object(), 0, max_depth)

    def find_elements(
        self, pid: int, role: str = "", name: str = "", value: str = ""
    ) -> list[UIElement]:
        tree = self.get_tree(pid, max_depth=8)
        if tree is None:
            return []
        results: list[UIElement] = []
        self._search(tree, role.lower(), name.lower(), value.lower(), results)
        return results

    def _search(self, el: UIElement, role: str, name: str, value: str, results: list):
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
        try:
            wrapper = element.platform_ref
            if action in ("press", "click"):
                wrapper.click_input()
            elif action == "invoke":
                wrapper.invoke()
            elif action == "select":
                wrapper.select()
            elif action == "expand":
                wrapper.expand()
            elif action == "collapse":
                wrapper.collapse()
            else:
                wrapper.click_input()
            return True
        except Exception:
            return False

    def focus_element(self, element: UIElement) -> bool:
        """Focus an element via UIA SetFocus()."""
        if element.platform_ref is None:
            return False
        try:
            element.platform_ref.set_focus()
            return True
        except Exception:
            return False

    def set_value(self, element: UIElement, value: str) -> bool:
        if element.platform_ref is None:
            return False
        try:
            wrapper = element.platform_ref
            wrapper.set_edit_text(value)
            return True
        except Exception:
            try:
                wrapper.type_keys(value, with_spaces=True)
                return True
            except Exception:
                return False

    def get_focused_element(self) -> UIElement | None:
        from pywinauto import Desktop

        self.reset_ids()
        desktop = Desktop(backend="uia")
        try:
            focused = desktop.get_focus()
            if focused:
                return self._wrapper_to_ui(focused, 0, 2)
        except Exception:
            pass
        return None
