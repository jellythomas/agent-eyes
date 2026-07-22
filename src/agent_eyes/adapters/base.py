"""Base adapter interface for platform accessibility APIs."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio",
    "combobox", "select", "tab", "menuitem", "slider",
    "switch", "searchbox", "spinbutton", "textarea",
})

_SECURE_STATES = frozenset({"password", "protected", "secure"})
_SECURE_ROLE_KEYS = frozenset({
    "password",
    "passwordfield",
    "passwordtext",
    "securetext",
    "securetextfield",
})


def _compact_field(value: str, limit: int) -> str:
    normalized = " ".join(value.split()).replace('"', '\\"')
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _normalized_role_key(role: str) -> str:
    return "".join(character for character in role.casefold() if character.isalnum())


@dataclass
class UIElement:
    """Universal UI element representation across all platforms."""
    id: int                          # Registry ID (assigned by registry)
    role: str                        # e.g. "button", "textfield", "heading"
    name: str = ""                   # Accessible name / title
    value: str = ""                  # Current value (for inputs, etc.)
    description: str = ""            # Accessible description
    states: list[str] = field(default_factory=list)  # e.g. ["focused", "enabled"]
    actions: list[str] = field(default_factory=list)  # e.g. ["press", "show_menu"]
    bounds: tuple[int, int, int, int] | None = None   # (x, y, width, height)
    children: list[UIElement] = field(default_factory=list)
    platform_ref: Any = None         # Native handle (AXUIElement, UIA element, etc.)
    source: str = "native"           # "native" or "cdp"
    visual: str = ""                 # Visual description (e.g. "red bg, white text, 14px")
    pid: int = 0                     # PID of the owning application (for window activation)
    tab_index: int = -1              # Chrome tab index for CDP elements (-1 = native/unknown)
    window_index: int = -1           # Native browser window index (-1 = unknown)

    def _is_secure(self) -> bool:
        return (
            _normalized_role_key(self.role) in _SECURE_ROLE_KEYS
            or any(str(state).casefold() in _SECURE_STATES for state in self.states)
        )

    def _location_label(self, viewport_w: int = 1920, viewport_h: int = 1080) -> str:
        """Compute spatial location label from bounds."""
        if not self.bounds:
            return ""
        x, y, w, h = self.bounds
        cx, cy = x + w // 2, y + h // 2
        row = "top" if cy < viewport_h // 3 else ("bottom" if cy > viewport_h * 2 // 3 else "center")
        col = "left" if cx < viewport_w // 3 else ("right" if cx > viewport_w * 2 // 3 else "center")
        if row == "center" and col == "center":
            return "center"
        if row == "center":
            return col
        if col == "center":
            return row
        return f"{row}-{col}"

    def to_text(self, depth: int = 0, max_depth: int = 10) -> str:
        """Render as numbered text for LLM consumption."""
        if depth > max_depth:
            return ""

        indent = "  " * depth
        parts = [f"[{self.id}]"]

        if self.role:
            parts.append(self.role)
        if self.name:
            parts.append(f'"{_compact_field(self.name, 120)}"')
        if self.value and not self._is_secure():
            val_preview = _compact_field(self.value, 80)
            parts.append(f'value="{val_preview}"')
        if self.states:
            parts.append(" ".join(self.states))
        if self.actions:
            parts.append(f'actions=[{", ".join(self.actions)}]')
        if self.bounds:
            x, y, w, h = self.bounds
            loc = self._location_label()
            parts.append(f"@{loc}({w}x{h})")
        if self.visual:
            parts.append(f"[{self.visual}]")

        line = f"{indent}{' '.join(parts)}"
        lines = [line]

        for child in self.children:
            child_text = child.to_text(depth + 1, max_depth)
            if child_text:
                lines.append(child_text)

        return "\n".join(lines)

    def to_flat_line(self) -> str:
        """Render as single flat line: [id] role "name" value="val" state1"""
        parts = [f"[{self.id}]", self.role]
        if self.name:
            parts.append(f'"{_compact_field(self.name, 120)}"')
        if self.value and not self._is_secure():
            parts.append(f'value="{_compact_field(self.value, 160)}"')
        skip_states = {"enabled"}
        meaningful = [s for s in self.states if s not in skip_states]
        parts.extend(meaningful)
        return " ".join(parts)


@dataclass
class AppInfo:
    """Information about a running application."""
    pid: int
    name: str
    bundle_id: str = ""
    windows: list[str] = field(default_factory=list)  # Window titles
    is_frontmost: bool = False


@runtime_checkable
class PlatformAdapter(Protocol):
    """Runtime-checkable Protocol for platform accessibility adapters.

    Every platform adapter (macOS, Linux, Windows) must implement all of
    these methods.  Use ``isinstance(adapter, PlatformAdapter)`` to verify
    at runtime that a given object satisfies the contract.
    """

    def is_available(self) -> bool: ...

    def check_permissions(self) -> tuple[bool, str]: ...

    def list_apps(self) -> list[AppInfo]: ...

    def get_tree(self, pid: int, max_depth: int = 5) -> UIElement | None: ...

    def get_browser_trees(self, pid: int, max_depth: int = 6) -> list[UIElement]: ...

    def get_subtree(self, element: UIElement, max_depth: int = 5) -> UIElement | None: ...

    def find_elements(
        self, pid: int, role: str = "", name: str = "", value: str = ""
    ) -> list[UIElement]: ...

    def perform_action(self, element: UIElement, action: str) -> bool: ...

    def focus_element(self, element: UIElement) -> bool: ...

    def is_same_element(self, first: UIElement, second: UIElement) -> bool: ...

    def is_element_valid(self, element: UIElement) -> bool: ...

    def element_at_position(self, x: float, y: float) -> UIElement | None: ...

    def focus_window(self, window: UIElement) -> bool: ...

    def is_window_focused(self, window: UIElement) -> bool: ...

    def is_element_selected(self, element: UIElement) -> bool: ...

    def set_value(self, element: UIElement, value: str) -> bool: ...

    def get_focused_element(self) -> UIElement | None: ...


class BaseAdapter(abc.ABC):
    """Abstract base for platform accessibility adapters."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this adapter can run on the current platform."""
        ...

    @abc.abstractmethod
    def check_permissions(self) -> tuple[bool, str]:
        """Check if accessibility permissions are granted.
        Returns (granted, message).
        """
        ...

    @abc.abstractmethod
    def list_apps(self) -> list[AppInfo]:
        """List running applications with visible windows."""
        ...

    @abc.abstractmethod
    def get_tree(self, pid: int, max_depth: int = 5) -> UIElement | None:
        """Get the accessibility tree for an application."""
        ...

    @abc.abstractmethod
    def get_browser_trees(self, pid: int, max_depth: int = 6) -> list[UIElement]:
        """Get every browser window tree while pruning page-document content."""
        ...

    @abc.abstractmethod
    def get_subtree(self, element: UIElement, max_depth: int = 5) -> UIElement | None:
        """Refresh a subtree from a previously returned live platform reference."""
        ...

    @abc.abstractmethod
    def find_elements(
        self, pid: int, role: str = "", name: str = "", value: str = ""
    ) -> list[UIElement]:
        """Search for elements matching criteria."""
        ...

    @abc.abstractmethod
    def perform_action(self, element: UIElement, action: str) -> bool:
        """Perform an action on an element (press, set_value, etc.)."""
        ...

    def focus_element(self, element: UIElement) -> bool:
        """Focus an element for keyboard input. Override per platform."""
        return False

    def is_same_element(self, first: UIElement, second: UIElement) -> bool:
        """Return native identity equality; the conservative default is identity."""
        return (
            first.platform_ref is not None
            and first.platform_ref is second.platform_ref
        )

    def is_element_valid(self, element: UIElement) -> bool:
        """Return whether a live native reference is still available."""
        return element.platform_ref is not None

    def element_at_position(self, x: float, y: float) -> UIElement | None:
        """Return the exact native element at screen coordinates when supported."""
        return None

    def focus_window(self, window: UIElement) -> bool:
        """Raise an exact native window reference when supported."""
        return False

    def is_window_focused(self, window: UIElement) -> bool:
        """Verify the exact native window is focused."""
        return False

    def is_element_selected(self, element: UIElement) -> bool:
        """Read whether a selectable element is currently active."""
        return "selected" in element.states or "focused" in element.states

    @abc.abstractmethod
    def set_value(self, element: UIElement, value: str) -> bool:
        """Set the value of an element (text fields, etc.)."""
        ...

    @abc.abstractmethod
    def get_focused_element(self) -> UIElement | None:
        """Get the currently focused element."""
        ...
