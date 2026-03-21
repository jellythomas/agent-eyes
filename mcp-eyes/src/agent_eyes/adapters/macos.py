"""macOS accessibility adapter using AXUIElement API via PyObjC."""
from __future__ import annotations

import functools
import re
import sys
from .base import BaseAdapter, UIElement, AppInfo


class MacOSAdapter(BaseAdapter):
    """macOS accessibility adapter using ApplicationServices framework."""

    def __init__(self):
        self._ax = None
        self._quartz = None
        self._cocoa = None
        self._id_counter = 0

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0
        self._in_web_area = False

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import ApplicationServices  # noqa: F401
            import Quartz  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        if self._ax is not None:
            return
        import ApplicationServices as ax
        import Quartz
        import Cocoa
        self._ax = ax
        self._quartz = Quartz
        self._cocoa = Cocoa

    def check_permissions(self) -> tuple[bool, str]:
        self._load()
        trusted = self._ax.AXIsProcessTrusted()
        if trusted:
            return True, "Accessibility permission granted."
        return False, (
            "Accessibility permission NOT granted. "
            "Go to System Settings > Privacy & Security > Accessibility "
            "and add your terminal app (Terminal, iTerm2, etc.)."
        )

    def list_apps(self) -> list[AppInfo]:
        self._load()
        workspace = self._cocoa.NSWorkspace.sharedWorkspace()
        running_apps = workspace.runningApplications()

        apps = []
        for app in running_apps:
            # Only include apps with a UI (regular activation policy)
            if app.activationPolicy() != self._cocoa.NSApplicationActivationPolicyRegular:
                continue

            pid = app.processIdentifier()
            name = app.localizedName() or "Unknown"
            bundle_id = app.bundleIdentifier() or ""
            is_front = app.isActive()

            # Get window titles via AX API
            windows = []
            ax_app = self._ax.AXUIElementCreateApplication(pid)
            err, ax_windows = self._ax.AXUIElementCopyAttributeValue(
                ax_app, "AXWindows", None
            )
            if err == 0 and ax_windows:
                for w in ax_windows:
                    err2, title = self._ax.AXUIElementCopyAttributeValue(
                        w, "AXTitle", None
                    )
                    if err2 == 0 and title:
                        windows.append(str(title))

            apps.append(AppInfo(
                pid=pid,
                name=name,
                bundle_id=bundle_id,
                windows=windows,
                is_frontmost=is_front,
            ))

        return sorted(apps, key=lambda a: (not a.is_frontmost, a.name))

    def _read_attr(self, element, attr: str) -> any:
        """Safely read an accessibility attribute."""
        err, val = self._ax.AXUIElementCopyAttributeValue(element, attr, None)
        if err == 0 and val is not None:
            return val
        return None

    # Roles worth exposing inside web content (interactive + structural)
    _WEB_INTERACTIVE_ROLES = frozenset({
        "button", "link", "textfield", "textarea", "combobox",
        "checkbox", "radiobutton", "slider", "menuitem", "tab",
        "searchfield", "popupbutton", "incrementor", "colorwell",
        "disclosuretriangle",
    })
    _WEB_STRUCTURAL_ROLES = frozenset({
        "heading", "list", "table", "row", "cell",
        "navigation", "main", "banner", "contentinfo",
        "form", "region", "alert", "dialog", "toolbar",
        "tabgroup", "menu", "menubar", "landmark",
    })
    _WEB_SKIP_ROLES = frozenset({
        "group", "statictext", "image", "separator", "unknown",
        "layoutarea", "layoutitem", "matte", "ruler",
        "rulermarker", "splitter", "growarea", "relevanceindicator",
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
        if role in self._WEB_SKIP_ROLES and meaningful_actions:
            return True
        return False

    # Attributes to batch-read per element (single IPC call instead of 10+)
    _BATCH_ATTRS = [
        "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXRoleDescription",
        "AXValue", "AXPlaceholderValue", "AXFocused", "AXEnabled", "AXSelected",
        "AXPosition", "AXSize", "AXChildren",
    ]

    def _batch_read_attrs(self, ax_el) -> dict:
        """Read all common attributes in a single IPC call (~5x faster)."""
        try:
            from Foundation import NSArray
            attrs = NSArray.arrayWithArray_(self._BATCH_ATTRS)
            err, values = self._ax.AXUIElementCopyMultipleAttributeValues(
                ax_el, attrs, 0, None  # 0 = don't stop on error
            )
            if err != 0 or values is None:
                return {}

            result = {}
            from CoreFoundation import kCFNull
            for i, attr in enumerate(self._BATCH_ATTRS):
                val = values[i]
                # kCFNull = attribute not supported; AXValueRef with 'error' = failed
                if val is kCFNull or (hasattr(val, '__class__') and 'AXValue' in type(val).__name__ and 'error' in str(val).lower()):
                    result[attr] = None
                else:
                    result[attr] = val
            return result
        except Exception:
            # Fallback to individual reads if batch API unavailable
            return {attr: self._read_attr(ax_el, attr) for attr in self._BATCH_ATTRS}

    # Hard cap on elements to prevent runaway traversal on complex pages
    _MAX_ELEMENTS = 1000
    _WEB_MAX_ELEMENTS = 3000

    def _element_to_ui(self, ax_el, depth: int, max_depth: int,
                       in_web_area: bool = False) -> UIElement | None:
        """Convert an AXUIElement to our UIElement format.

        Uses AXUIElementCopyMultipleAttributeValues for ~5x faster traversal
        by batching attribute reads into a single IPC round-trip per element.
        """
        if depth > max_depth:
            return None
        cap = self._WEB_MAX_ELEMENTS if self._in_web_area else self._MAX_ELEMENTS
        if self._id_counter >= cap:
            return None

        # Single IPC call for all attributes (~1.3ms vs ~11ms for individual calls)
        attrs = self._batch_read_attrs(ax_el)

        role_raw = attrs.get("AXRole") or ""
        role = str(role_raw).replace("AX", "").lower() if role_raw else ""

        # Skip ignored/invisible elements
        if role in ("unknown", ""):
            return None

        # Detect entry into web content — dynamically extend depth
        if role == "webarea":
            in_web_area = True
            self._in_web_area = True
            # Web content needs deeper traversal. Extend depth budget by 20
            # from current position (buttons are 3-5 levels below AXWebArea).
            max_depth = max(max_depth, depth + 20)

        name = str(attrs.get("AXTitle") or "")
        if not name:
            name = str(attrs.get("AXDescription") or "")
        if not name:
            name = str(attrs.get("AXPlaceholderValue") or "")
        if not name:
            name = str(attrs.get("AXRoleDescription") or "")

        value_raw = attrs.get("AXValue")
        value = str(value_raw)[:200] if value_raw is not None else ""

        # States (from batch-read values)
        states = []
        if attrs.get("AXFocused"):
            states.append("focused")
        if attrs.get("AXEnabled") is False:
            states.append("disabled")
        if attrs.get("AXSelected"):
            states.append("selected")
        # Detect secure text fields (NSSecureTextField) — these block CGEvent
        # keyboard injection via EnableSecureEventInput(). Must use set_value.
        subrole = str(attrs.get("AXSubrole") or "")
        if "securetextfield" in subrole.lower().replace("ax", ""):
            states.append("secure")

        # Actions (still a separate call — no batch API for this)
        actions = []
        err, action_names = self._ax.AXUIElementCopyActionNames(ax_el, None)
        if err == 0 and action_names:
            actions = [str(a).replace("AX", "").lower() for a in action_names]

        # Bounds (from batch-read position/size)
        bounds = None
        pos = attrs.get("AXPosition")
        size = attrs.get("AXSize")
        if pos is not None and size is not None:
            try:
                bounds = self._parse_bounds(pos, size)
            except Exception:
                pass

        element = UIElement(
            id=self._next_id(),
            role=role,
            name=name,
            value=value,
            states=states,
            actions=actions,
            bounds=bounds,
            platform_ref=ax_el,
            source="native",
        )

        # Reuse children from batch read (avoids extra IPC call)
        children_raw = attrs.get("AXChildren")
        # Ensure children is actually iterable (not an error sentinel)
        if children_raw is not None and not hasattr(children_raw, '__iter__'):
            children_raw = None

        # Web filtering: inside AXWebArea, only keep interactive/structural elements
        if in_web_area and role != "webarea":
            if not self._should_include_web_element(role, name, actions):
                # Still recurse children — a skipped <div> may contain buttons
                if depth < max_depth and children_raw:
                    for child_ax in children_raw:
                        child = self._element_to_ui(
                            child_ax, depth + 1, max_depth,
                            in_web_area=True,
                        )
                        if child:
                            element.children.append(child)
                if element.children:
                    return element
                return None

        # Recurse children
        if depth < max_depth and children_raw:
            for child_ax in children_raw:
                child = self._element_to_ui(
                    child_ax, depth + 1, max_depth,
                    in_web_area=in_web_area,
                )
                if child:
                    element.children.append(child)

        return element

    def force_browser_accessibility(self, pid: int) -> None:
        """Tell the browser an assistive technology is connected.

        This forces Chrome/Chromium to build its full accessibility tree
        (normally lazily constructed). Safe to call on any app — no-op if
        the attribute is unsupported.
        """
        self._load()
        ax_app = self._ax.AXUIElementCreateApplication(pid)
        try:
            self._ax.AXUIElementSetAttributeValue(
                ax_app, "AXEnhancedUserInterface", True
            )
        except Exception:
            pass  # Not all apps support this — that's fine

    def get_tree(self, pid: int, max_depth: int = 5,
                 is_browser: bool = False) -> UIElement | None:
        self._load()
        self.reset_ids()
        ax_app = self._ax.AXUIElementCreateApplication(pid)

        # Force ALL apps to enable accessibility tree construction.
        # Safe no-op on native apps (returns kAXErrorNotImplemented).
        # Critical for Electron/CEF/WebKit apps that lazily build AX trees.
        self.force_browser_accessibility(pid)

        # Try focused window first, fall back to first window
        ax_window = self._read_attr(ax_app, "AXFocusedWindow")
        if ax_window is None:
            windows = self._read_attr(ax_app, "AXWindows")
            if windows and len(windows) > 0:
                ax_window = windows[0]

        if ax_window is None:
            # Return app-level tree
            return self._element_to_ui(ax_app, 0, max_depth)

        return self._element_to_ui(ax_window, 0, max_depth)

    def find_elements(
        self, pid: int, role: str = "", name: str = "", value: str = ""
    ) -> list[UIElement]:
        self._load()
        tree = self.get_tree(pid, max_depth=8)
        if tree is None:
            return []

        results = []
        self._search(tree, role.lower(), name.lower(), value.lower(), results)
        return results

    def _search(
        self, el: UIElement, role: str, name: str, value: str, results: list
    ):
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

    _RE_POS = re.compile(r'x:([\d.]+)\s+y:([\d.]+)')
    _RE_SIZE = re.compile(r'w:([\d.]+)\s+h:([\d.]+)')

    @classmethod
    def _parse_bounds(cls, pos_val, size_val) -> tuple[int, int, int, int] | None:
        """Extract bounds from AXValue objects via string representation."""
        m_pos = cls._RE_POS.search(str(pos_val))
        m_size = cls._RE_SIZE.search(str(size_val))
        if m_pos and m_size:
            return (
                int(float(m_pos.group(1))),
                int(float(m_pos.group(2))),
                int(float(m_size.group(1))),
                int(float(m_size.group(2))),
            )
        return None

    def perform_action(self, element: UIElement, action: str) -> bool:
        self._load()
        if element.platform_ref is None:
            return False
        ax_action = f"AX{action.capitalize()}"
        err = self._ax.AXUIElementPerformAction(element.platform_ref, ax_action)
        return err == 0

    def focus_element(self, element: UIElement) -> bool:
        """Focus an element by setting AXFocused=True."""
        self._load()
        if element.platform_ref is None:
            return False
        err = self._ax.AXUIElementSetAttributeValue(
            element.platform_ref, "AXFocused", True
        )
        return err == 0

    def set_value(self, element: UIElement, value: str) -> bool:
        self._load()
        if element.platform_ref is None:
            return False
        err = self._ax.AXUIElementSetAttributeValue(
            element.platform_ref, "AXValue", value
        )
        return err == 0

    def get_focused_element(self) -> UIElement | None:
        self._load()
        self.reset_ids()
        system = self._ax.AXUIElementCreateSystemWide()
        focused = self._read_attr(system, "AXFocusedUIElement")
        if focused is None:
            return None
        return self._element_to_ui(focused, 0, 2)
