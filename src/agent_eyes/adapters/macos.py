"""macOS accessibility adapter using AXUIElement API via PyObjC."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from .base import BaseAdapter, UIElement, AppInfo


class MacOSAdapter(BaseAdapter):
    """macOS accessibility adapter using ApplicationServices framework."""

    def __init__(self):
        self._ax = None
        self._quartz = None
        self._cocoa = None
        self._id_counter = 0
        self._include_web_content = True
        self._traversed_elements = 0
        self._visited_element_keys: set[int] = set()
        self._strict_reads = False
        self._window_server_visible_pids: set[int] | None = None
        self._window_server_required_window_counts: dict[int, int] | None = None

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0
        self._reset_traversal_state()

    def _reset_traversal_state(self) -> None:
        self._in_web_area = False
        self._traversed_elements = 0
        self._visited_element_keys.clear()

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
            "and enable the app that launches Agent Eyes "
            "(for example Codex, Claude, Terminal, or iTerm)."
        )

    def list_apps(self) -> list[AppInfo]:
        self._load()
        workspace = self._cocoa.NSWorkspace.sharedWorkspace()
        running_apps = workspace.runningApplications()
        window_titles = self._window_titles_by_pid()

        apps = []
        for app in running_apps:
            # Only include apps with a UI (regular activation policy)
            if app.activationPolicy() != self._cocoa.NSApplicationActivationPolicyRegular:
                continue

            pid = app.processIdentifier()
            name = app.localizedName() or "Unknown"
            bundle_id = app.bundleIdentifier() or ""
            is_front = app.isActive()

            # One WindowServer snapshot supplies titles for every process.
            # Fall back to AX only when the snapshot itself is unavailable
            # (for example when window metadata is restricted by the OS).
            windows = (
                list(window_titles.get(pid, ()))
                if window_titles is not None
                else self._ax_window_titles(pid)
            )

            apps.append(AppInfo(
                pid=pid,
                name=name,
                bundle_id=bundle_id,
                windows=windows,
                is_frontmost=is_front,
            ))

        return sorted(apps, key=lambda a: (not a.is_frontmost, a.name))

    def list_apps_complete(self) -> list[AppInfo]:
        """Return app inventory or surface AX fallback failures to safe callers."""
        previous = self._strict_reads
        self._strict_reads = True
        try:
            return self.list_apps()
        finally:
            self._strict_reads = previous

    def browser_app_has_visible_windows(self, app: AppInfo) -> bool | None:
        """Use the last complete WindowServer snapshot, including untitled windows."""
        visible_pids = self._window_server_visible_pids
        return None if visible_pids is None else app.pid in visible_pids

    def browser_app_required_window_count(self, app: AppInfo) -> int | None:
        """Count distinct titled or on-screen normal WindowServer windows."""
        counts = self._window_server_required_window_counts
        return None if counts is None else counts.get(app.pid, 0)

    def _window_titles_by_pid(self) -> dict[int, list[str]] | None:
        """Read all normal-window titles with one WindowServer call."""
        self._window_server_visible_pids = None
        self._window_server_required_window_counts = None
        try:
            records = self._quartz.CGWindowListCopyWindowInfo(
                self._quartz.kCGWindowListOptionAll,
                self._quartz.kCGNullWindowID,
            )
        except Exception:
            return None
        if records is None:
            return None

        titles: dict[int, list[str]] = defaultdict(list)
        visible_pids: set[int] = set()
        required_window_keys: dict[int, set[tuple[object, ...]]] = defaultdict(set)
        saw_title = False
        saw_on_screen = False
        for record in records:
            try:
                if int(record.get(self._quartz.kCGWindowLayer, -1)) != 0:
                    continue
                pid = int(record.get(self._quartz.kCGWindowOwnerPID, 0))
                title = str(record.get(self._quartz.kCGWindowName, "") or "").strip()
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            visible_pids.add(pid)
            window_number_key = getattr(self._quartz, "kCGWindowNumber", None)
            window_number = (
                record.get(window_number_key) if window_number_key is not None else None
            )
            if window_number is not None:
                identity: tuple[object, ...] = ("window", window_number)
            else:
                bounds_key = getattr(self._quartz, "kCGWindowBounds", None)
                bounds = record.get(bounds_key, {}) if bounds_key is not None else {}
                identity = ("fallback", title, repr(bounds))

            on_screen_key = getattr(self._quartz, "kCGWindowIsOnscreen", None)
            on_screen = bool(
                record.get(on_screen_key, False) if on_screen_key is not None else False
            )
            if on_screen:
                saw_on_screen = True
                required_window_keys[pid].add(identity)
            if title:
                saw_title = True
                required_window_keys[pid].add(identity)
                if title not in titles[pid]:
                    titles[pid].append(title)

        # An entirely title-less result usually means metadata access is not
        # available. Preserve the AX fallback in that environment.
        self._window_server_visible_pids = visible_pids if saw_title else None
        self._window_server_required_window_counts = (
            {pid: len(keys) for pid, keys in required_window_keys.items()}
            if saw_title or saw_on_screen
            else None
        )
        return dict(titles) if saw_title else None

    def _ax_window_titles(self, pid: int) -> list[str]:
        """Fallback title lookup for systems without WindowServer metadata."""
        ax_app = self._ax.AXUIElementCreateApplication(pid)
        windows = self._read_attr(ax_app, "AXWindows") or []
        titles = []
        for window in self._as_sequence(windows):
            if self._normalized_role(window) == "application":
                continue
            title = self._read_attr(window, "AXTitle")
            clean_title = str(title or "").strip()
            if clean_title and clean_title not in titles:
                titles.append(clean_title)
        return titles

    def _read_attr(self, element, attr: str) -> any:
        """Safely read an accessibility attribute."""
        try:
            err, val = self._ax.AXUIElementCopyAttributeValue(element, attr, None)
        except Exception as exc:
            if self._strict_reads:
                raise RuntimeError(
                    f"macOS accessibility read failed for {attr}"
                ) from exc
            return None
        if err == 0 and val is not None:
            return val
        if self._strict_reads and err != 0:
            raise RuntimeError(
                f"macOS accessibility read failed for {attr} (error {err})"
            )
        return None

    @staticmethod
    def _as_sequence(value) -> list:
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            return []
        try:
            return list(value)
        except TypeError:
            return [value]

    def _normalized_role(self, element) -> str:
        raw_role = self._read_attr(element, "AXRole")
        return str(raw_role or "").removeprefix("AX").casefold()

    @staticmethod
    def _element_key(element) -> int:
        try:
            return hash(element)
        except TypeError:
            return id(element)

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
    _BROWSER_PRUNE_ROLES = frozenset({
        "menu", "menubar", "menubaritem", "menuitem",
    })
    _BROWSER_DOCUMENT_ROLES = frozenset({
        "document", "documentframe", "documentweb", "webarea",
    })
    _BROWSER_ACTION_ROLES = frozenset({
        "button", "pagetab", "radio", "radiobutton", "tab", "tabitem",
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
                if self._strict_reads:
                    raise RuntimeError(
                        f"macOS accessibility batch read failed (error {err})"
                    )
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
            if self._strict_reads:
                raise
            # Fallback to individual reads if batch API unavailable
            return {attr: self._read_attr(ax_el, attr) for attr in self._BATCH_ATTRS}

    # Hard cap on elements to prevent runaway traversal on complex pages
    _MAX_ELEMENTS = 1000
    _WEB_MAX_ELEMENTS = 3000
    _BROWSER_MAX_ELEMENTS = 500

    def _element_to_ui(self, ax_el, depth: int, max_depth: int,
                       in_web_area: bool = False) -> UIElement | None:
        """Convert an AXUIElement to our UIElement format.

        Uses AXUIElementCopyMultipleAttributeValues for ~5x faster traversal
        by batching attribute reads into a single IPC round-trip per element.
        """
        if depth > max_depth:
            return None

        element_key = self._element_key(ax_el)
        if element_key in self._visited_element_keys:
            return None

        browser_inventory = not getattr(self, "_include_web_content", True)
        if browser_inventory:
            cap = self._BROWSER_MAX_ELEMENTS
        else:
            cap = self._WEB_MAX_ELEMENTS if self._in_web_area else self._MAX_ELEMENTS
        if self._traversed_elements >= cap:
            if self._strict_reads:
                raise RuntimeError(
                    "macOS AX browser inventory exceeded its traversal safety cap"
                )
            return None
        self._visited_element_keys.add(element_key)
        self._traversed_elements += 1

        # Single IPC call for all attributes (~1.3ms vs ~11ms for individual calls)
        attrs = self._batch_read_attrs(ax_el)

        role_raw = attrs.get("AXRole") or ""
        role = str(role_raw).replace("AX", "").lower() if role_raw else ""

        # Skip ignored/invisible elements
        if role in ("unknown", ""):
            return None

        # Browser inventory never needs application menus. Avoid both the
        # output noise and hundreds of native IPC calls below the menu bar.
        if browser_inventory and role in self._BROWSER_PRUNE_ROLES:
            return None

        # Detect entry into web content — dynamically extend depth
        if role == "webarea" or (
            browser_inventory and role in self._BROWSER_DOCUMENT_ROLES
        ):
            in_web_area = True
            self._in_web_area = True
            if not browser_inventory:
                # Web content needs deeper traversal. Extend depth budget by 20
                # from current position (buttons are 3-5 levels below AXWebArea).
                max_depth = max(max_depth, depth + 20)
            else:
                # Browser inventory needs only browser chrome. Keep the web-area
                # node as a boundary marker and never traverse page content.
                max_depth = depth

        name = str(attrs.get("AXTitle") or "")
        if not name:
            name = str(attrs.get("AXDescription") or "")
        if not name:
            name = str(attrs.get("AXPlaceholderValue") or "")
        if not name:
            name = str(attrs.get("AXRoleDescription") or "")

        subrole = str(attrs.get("AXSubrole") or "")
        is_secure = "securetextfield" in subrole.casefold().replace("ax", "")
        value_raw = attrs.get("AXValue")
        value = (
            str(value_raw)[:200]
            if value_raw is not None and not is_secure
            else ""
        )

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
        if is_secure:
            states.append("secure")

        # Actions (still a separate call — no batch API for this)
        actions = []
        if not browser_inventory or role in self._BROWSER_ACTION_ROLES:
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
                        try:
                            child = self._element_to_ui(
                                child_ax, depth + 1, max_depth,
                                in_web_area=True,
                            )
                            if child:
                                element.children.append(child)
                        except Exception:
                            if self._strict_reads:
                                raise
                            continue  # Skip crashed/invalid child elements
                if element.children:
                    return element
                return None

        # Recurse children
        if depth < max_depth and children_raw:
            for child_ax in children_raw:
                try:
                    child = self._element_to_ui(
                        child_ax, depth + 1, max_depth,
                        in_web_area=in_web_area,
                    )
                    if child:
                        element.children.append(child)
                except Exception:
                    if self._strict_reads:
                        raise
                    continue  # Skip crashed/invalid child elements
        elif (
            self._strict_reads
            and browser_inventory
            and children_raw
            and role not in self._BROWSER_DOCUMENT_ROLES
        ):
            raise RuntimeError(
                "macOS AX browser inventory exceeded its traversal depth bound"
            )

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

    def is_element_valid(self, element) -> bool:
        """Check if an element's AX reference is still valid (not destroyed)."""
        if element.platform_ref is None:
            return False
        try:
            err, _ = self._ax.AXUIElementCopyAttributeValue(element.platform_ref, "AXRole", None)
            return err == 0
        except Exception:
            return False

    def get_tree(self, pid: int, max_depth: int = 5,
                 is_browser: bool = False) -> UIElement | None:
        self._load()
        self._include_web_content = True
        self.reset_ids()
        ax_app = self._ax.AXUIElementCreateApplication(pid)

        # Force browser accessibility tree construction.
        # Only needed for Chromium-based apps — skip for native apps.
        from ..platform_utils import is_browser_pid
        if is_browser_pid(pid):
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

    def get_browser_trees(self, pid: int, max_depth: int = 6) -> list[UIElement]:
        """Return all browser window trees without traversing web documents."""
        self._load()
        self.reset_ids()
        self._include_web_content = False
        ax_app = self._ax.AXUIElementCreateApplication(pid)

        from ..platform_utils import is_browser_pid
        if is_browser_pid(pid):
            self.force_browser_accessibility(pid)

        try:
            windows = self._browser_window_roots(ax_app)
            trees = []
            for window_index, window in windows:
                # Bound each window independently so one complex window cannot
                # consume the traversal budget for every later window.
                self._reset_traversal_state()
                tree = self._element_to_ui(window, 0, max_depth)
                if tree is not None:
                    tree.window_index = window_index
                    trees.append(tree)
            return trees
        finally:
            self._include_web_content = True

    def get_browser_trees_complete(
        self,
        pid: int,
        max_depth: int = 6,
    ) -> list[UIElement]:
        """Return every browser tree or raise on any collapsed AX read failure."""
        previous = self._strict_reads
        self._strict_reads = True
        try:
            return self.get_browser_trees(pid, max_depth=max_depth)
        finally:
            self._strict_reads = previous

    def get_subtree(self, element: UIElement, max_depth: int = 5) -> UIElement | None:
        if element.platform_ref is None:
            return None
        self._load()
        self._include_web_content = True
        self.reset_ids()
        return self._element_to_ui(element.platform_ref, 0, max_depth)

    def is_element_selected(self, element: UIElement) -> bool:
        if element.platform_ref is None:
            return False
        self._load()
        try:
            if bool(self._read_attr(element.platform_ref, "AXSelected")):
                return True
            if bool(self._read_attr(element.platform_ref, "AXFocused")):
                return True
            value = self._read_attr(element.platform_ref, "AXValue")
            return isinstance(value, (bool, int, float)) and bool(value)
        except Exception:
            return False

    def _browser_window_roots(self, ax_app) -> list[tuple[int, object]]:
        """Return unique AX window roots, rejecting app/menu proxy cycles."""
        roots: list[tuple[int, object]] = []
        seen: set[int] = set()

        def add(index: int, candidate) -> None:
            if candidate is None:
                return
            key = self._element_key(candidate)
            if key in seen:
                return
            role = self._normalized_role(candidate)
            if role in {"", "application", "menu", "menubar", "menuitem"}:
                return
            seen.add(key)
            roots.append((index, candidate))

        for index, window in enumerate(
            self._as_sequence(self._read_attr(ax_app, "AXWindows"))
        ):
            add(index, window)

        if not roots:
            add(0, self._read_attr(ax_app, "AXFocusedWindow"))
            add(0, self._read_attr(ax_app, "AXMainWindow"))
        return roots

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

    def is_same_element(self, first: UIElement, second: UIElement) -> bool:
        """Compare AXUIElement identity through Core Foundation."""
        self._load()
        if first.platform_ref is None or second.platform_ref is None:
            return False
        try:
            return bool(self._ax.CFEqual(first.platform_ref, second.platform_ref))
        except Exception:
            return False

    def focus_window(self, window: UIElement) -> bool:
        """Raise one exact AX window reference."""
        self._load()
        if window.platform_ref is None:
            return False
        try:
            return self._ax.AXUIElementPerformAction(
                window.platform_ref,
                "AXRaise",
            ) == 0
        except Exception:
            return False

    def is_window_focused(self, window: UIElement) -> bool:
        """Compare the app's AXFocusedWindow with the requested window."""
        self._load()
        if window.platform_ref is None:
            return False
        try:
            err, pid = self._ax.AXUIElementGetPid(window.platform_ref, None)
            if err != 0 or not pid:
                return False
            app = self._ax.AXUIElementCreateApplication(pid)
            focused = self._read_attr(app, "AXFocusedWindow")
            return focused is not None and bool(
                self._ax.CFEqual(window.platform_ref, focused)
            )
        except Exception:
            return False

    def set_value(self, element: UIElement, value: str) -> bool:
        self._load()
        if element.platform_ref is None:
            return False
        err = self._ax.AXUIElementSetAttributeValue(
            element.platform_ref, "AXValue", value
        )
        return err == 0

    def element_at_position(self, x: float, y: float) -> UIElement | None:
        """Get the UI element at the given screen coordinates."""
        self._load()
        system_wide = self._ax.AXUIElementCreateSystemWide()
        err, ax_el = self._ax.AXUIElementCopyElementAtPosition(system_wide, float(x), float(y), None)
        if err != 0 or ax_el is None:
            return None

        # Read basic attributes
        attrs = self._batch_read_attrs(ax_el)
        role_raw = attrs.get("AXRole") or ""
        role = str(role_raw).replace("AX", "").lower() if role_raw else "unknown"

        name = str(attrs.get("AXTitle") or "")
        if not name:
            name = str(attrs.get("AXDescription") or "")
        if not name:
            name = str(attrs.get("AXPlaceholderValue") or "")
        if not name:
            name = str(attrs.get("AXRoleDescription") or "")

        value_raw = attrs.get("AXValue")
        value = str(value_raw)[:200] if value_raw is not None else ""

        bounds = None
        pos = attrs.get("AXPosition")
        size = attrs.get("AXSize")
        if pos is not None and size is not None:
            try:
                bounds = self._parse_bounds(pos, size)
            except Exception:
                pass

        actions = []
        err2, action_names = self._ax.AXUIElementCopyActionNames(ax_el, None)
        if err2 == 0 and action_names:
            actions = [str(a).replace("AX", "").lower() for a in action_names]

        # Get owning PID
        err3, pid = self._ax.AXUIElementGetPid(ax_el, None)
        element_pid = pid if err3 == 0 else 0

        element = UIElement(
            id=self._next_id(),
            role=role,
            name=name,
            value=value,
            actions=actions,
            bounds=bounds,
            platform_ref=ax_el,
            source="native",
            pid=element_pid,
        )
        return element

    def get_focused_element(self) -> UIElement | None:
        self._load()
        self.reset_ids()
        system = self._ax.AXUIElementCreateSystemWide()
        focused = self._read_attr(system, "AXFocusedUIElement")
        if focused is None:
            return None
        return self._element_to_ui(focused, 0, 2)
