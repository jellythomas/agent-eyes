"""Linux accessibility adapter using AT-SPI2 via GObject Introspection."""
from __future__ import annotations

import sys
from .base import BaseAdapter, UIElement, AppInfo


class LinuxAdapter(BaseAdapter):
    """Linux AT-SPI2 accessibility adapter."""

    _MAX_ELEMENTS = 1000
    _WEB_MAX_ELEMENTS = 3000

    def __init__(self):
        self._atspi = None
        self._id_counter = 0

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0
        self._in_web_area = False

    def is_available(self) -> bool:
        if sys.platform != "linux":
            return False
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # noqa: F401
            return True
        except (ImportError, ValueError):
            return False

    def _load(self):
        if self._atspi is not None:
            return
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self._atspi = Atspi

    def check_permissions(self) -> tuple[bool, str]:
        try:
            self._load()
            # AT-SPI2 typically works without special permissions
            # but the AT-SPI2 daemon must be running
            desktop = self._atspi.get_desktop(0)
            if desktop is not None:
                return True, "AT-SPI2 accessibility service is running."
            return False, "AT-SPI2 desktop object not found. Is at-spi2-core running?"
        except Exception as e:
            return False, f"AT-SPI2 not available: {e}. Install: apt install at-spi2-core python3-gi"

    def list_apps(self) -> list[AppInfo]:
        self._load()
        desktop = self._atspi.get_desktop(0)
        apps = []

        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                if app is None:
                    continue

                name = app.get_name() or "Unknown"
                pid = app.get_process_id()

                # Get window titles
                windows = []
                for j in range(app.get_child_count()):
                    win = app.get_child_at_index(j)
                    if win:
                        title = win.get_name()
                        if title:
                            windows.append(title)

                apps.append(AppInfo(
                    pid=pid,
                    name=name,
                    windows=windows,
                ))
            except Exception:
                continue

        return apps

    def get_tree(self, pid: int, max_depth: int = 5,
                 is_browser: bool = False) -> UIElement | None:
        self._load()
        self.reset_ids()
        desktop = self._atspi.get_desktop(0)

        # Find app by PID
        target_app = None
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app and app.get_process_id() == pid:
                target_app = app
                break

        if target_app is None:
            return None

        # Get first window or app itself
        if target_app.get_child_count() > 0:
            window = target_app.get_child_at_index(0)
            if window:
                return self._atspi_to_ui(window, 0, max_depth)

        return self._atspi_to_ui(target_app, 0, max_depth)

    # Roles worth exposing inside web content (interactive + structural)
    _WEB_INTERACTIVE_ROLES = frozenset({
        "push button", "link", "entry", "combo box", "check box",
        "radio button", "slider", "menu item", "page tab", "toggle button",
        "spin button", "password text",
    })
    _WEB_STRUCTURAL_ROLES = frozenset({
        "heading", "list", "list item", "table", "table cell", "table row",
        "document web", "document frame", "form", "landmark",
        "navigation", "dialog", "alert", "toolbar", "menu bar",
    })

    def _should_include_web_element(self, role: str, name: str, actions: list) -> bool:
        """Filter web elements: keep interactive + named structural, skip noise."""
        if role in self._WEB_INTERACTIVE_ROLES:
            return True
        if role in self._WEB_STRUCTURAL_ROLES and name:
            return True
        # Only count meaningful actions (press, click, confirm).
        # scrolltovisible/showmenu are Chrome defaults on every DOM element — not real interactivity.
        meaningful_actions = [a for a in actions if a not in ("scrolltovisible", "showmenu")]
        if meaningful_actions:
            return True
        return False

    def _atspi_to_ui(self, obj, depth: int, max_depth: int,
                     in_web_area: bool = False) -> UIElement | None:
        if depth > max_depth or obj is None:
            return None
        cap = self._WEB_MAX_ELEMENTS if self._in_web_area else self._MAX_ELEMENTS
        if self._id_counter >= cap:
            return None

        try:
            role = obj.get_role_name() or "unknown"
            name = obj.get_name() or ""

            # Detect entry into web content
            if role in ("document web", "document frame"):
                in_web_area = True
                self._in_web_area = True
                max_depth = max(max_depth, depth + 20)
            description = obj.get_description() or ""

            # Value
            value = ""
            try:
                val_iface = obj.get_value()
                if val_iface:
                    value = str(val_iface.get_current_value())
            except Exception:
                pass
            if not value:
                try:
                    text_iface = obj.get_text()
                    if text_iface:
                        char_count = text_iface.get_character_count()
                        if char_count > 0:
                            value = text_iface.get_text(0, min(char_count, 200))
                except Exception:
                    pass

            # States
            states = []
            try:
                state_set = obj.get_state_set()
                if state_set.contains(self._atspi.StateType.FOCUSED):
                    states.append("focused")
                if state_set.contains(self._atspi.StateType.SELECTED):
                    states.append("selected")
                if not state_set.contains(self._atspi.StateType.ENABLED):
                    states.append("disabled")
                if not state_set.contains(self._atspi.StateType.VISIBLE):
                    return None  # Skip invisible elements
            except Exception:
                pass

            # Actions
            actions = []
            try:
                action_iface = obj.get_action()
                if action_iface:
                    for k in range(action_iface.get_n_actions()):
                        act_name = action_iface.get_action_name(k)
                        if act_name:
                            actions.append(act_name)
            except Exception:
                pass

            # Bounds
            bounds = None
            try:
                comp = obj.get_component()
                if comp:
                    ext = comp.get_extents(self._atspi.CoordType.SCREEN)
                    bounds = (ext.x, ext.y, ext.width, ext.height)
            except Exception:
                pass

            element = UIElement(
                id=self._next_id(),
                role=role,
                name=name,
                value=value,
                description=description,
                states=states,
                actions=actions,
                bounds=bounds,
                platform_ref=obj,
                source="native",
            )

            # Web filtering: inside document, only keep interactive/structural
            if in_web_area and role not in ("document web", "document frame"):
                if not self._should_include_web_element(role, name, actions):
                    if depth < max_depth:
                        for i in range(obj.get_child_count()):
                            child = obj.get_child_at_index(i)
                            child_ui = self._atspi_to_ui(
                                child, depth + 1, max_depth, in_web_area=True,
                            )
                            if child_ui:
                                element.children.append(child_ui)
                    if element.children:
                        return element
                    return None

            if depth < max_depth:
                for i in range(obj.get_child_count()):
                    child = obj.get_child_at_index(i)
                    child_ui = self._atspi_to_ui(
                        child, depth + 1, max_depth, in_web_area=in_web_area,
                    )
                    if child_ui:
                        element.children.append(child_ui)

            return element
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
            obj = element.platform_ref
            action_iface = obj.get_action()
            if action_iface:
                for i in range(action_iface.get_n_actions()):
                    if action_iface.get_action_name(i) == action:
                        return action_iface.do_action(i)
                # Default: try first action
                if action in ("press", "click") and action_iface.get_n_actions() > 0:
                    return action_iface.do_action(0)
            return False
        except Exception:
            return False

    def focus_element(self, element: UIElement) -> bool:
        """Focus an element via AT-SPI2 Component.grab_focus()."""
        if element.platform_ref is None:
            return False
        try:
            component = element.platform_ref.get_component()
            if component:
                return component.grab_focus()
            return False
        except Exception:
            return False

    def set_value(self, element: UIElement, value: str) -> bool:
        if element.platform_ref is None:
            return False
        try:
            obj = element.platform_ref
            # Try editable text interface
            text_iface = obj.get_editable_text()
            if text_iface:
                # Clear and set
                char_count = obj.get_text().get_character_count()
                if char_count > 0:
                    text_iface.delete_text(0, char_count)
                text_iface.insert_text(0, value, len(value))
                return True

            # Try value interface
            val_iface = obj.get_value()
            if val_iface:
                val_iface.set_current_value(float(value))
                return True

            return False
        except Exception:
            return False

    def get_focused_element(self) -> UIElement | None:
        self._load()
        self.reset_ids()
        desktop = self._atspi.get_desktop(0)

        # Walk apps to find focused element
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            focused = self._find_focused(app, 0, 5)
            if focused:
                return focused
        return None

    def _find_focused(self, obj, depth: int, max_depth: int) -> UIElement | None:
        if depth > max_depth or obj is None:
            return None
        try:
            state_set = obj.get_state_set()
            if state_set.contains(self._atspi.StateType.FOCUSED):
                return self._atspi_to_ui(obj, 0, 2)
            for i in range(obj.get_child_count()):
                child = obj.get_child_at_index(i)
                result = self._find_focused(child, depth + 1, max_depth)
                if result:
                    return result
        except Exception:
            pass
        return None
