"""Unified Element Registry — maps integer IDs to live platform references.

This is the bridge between the text representation sent to Claude
and the actual native/CDP elements that can be acted upon.
"""
from __future__ import annotations

import time

from .adapters.base import UIElement


# TTL for registered elements (seconds)
_REGISTRY_TTL_SECONDS = 60


class ElementRegistry:
    """Maps element IDs to their UIElement objects for action dispatch."""

    def __init__(self):
        self._elements: dict[int, UIElement] = {}
        self.last_pid: int = 0  # PID from the last get_tree call
        self.last_tab_index: int = -1  # Tab index from the last get_web_tree call
        self._registered_at: float = 0  # Timestamp when elements were registered

    def clear(self):
        self._elements.clear()
        self._registered_at = 0

    def register_tree(self, root: UIElement, pid: int = 0, tab_index: int = -1):
        """Walk a tree and register all elements, tagging each with the owning PID and tab_index."""
        self.clear()
        self.last_pid = pid
        self.last_tab_index = tab_index
        self._registered_at = time.time()
        self._walk_and_register(root, pid, tab_index)

    def _walk_and_register(self, element: UIElement, pid: int = 0, tab_index: int = -1):
        if pid:
            element.pid = pid
        if tab_index >= 0:
            element.tab_index = tab_index
        self._elements[element.id] = element
        for child in element.children:
            self._walk_and_register(child, pid, tab_index)

    def is_expired(self) -> bool:
        """Check if the registry has expired (elements are stale)."""
        if self._registered_at == 0:
            return True  # Never registered
        return (time.time() - self._registered_at) > _REGISTRY_TTL_SECONDS

    def get(self, element_id: int) -> UIElement | None:
        # Note: We don't automatically return None for expired elements here
        # because the caller should handle TTL checks to give better error messages.
        # Use is_expired() to check before batch operations.
        return self._elements.get(element_id)

    def get_with_ttl_check(self, element_id: int) -> tuple[UIElement | None, bool]:
        """Get element with TTL expiration check.
        Returns (element, is_expired). If expired, element may still be returned
        but caller should warn user to refresh.
        """
        element = self._elements.get(element_id)
        return element, self.is_expired()

    def register_element(self, element: "UIElement") -> None:
        """Register a single element without clearing the full registry.

        Updates the TTL timestamp so the element is not immediately stale.
        Use this instead of writing to _elements directly.
        """
        if self._registered_at == 0:
            self._registered_at = time.time()
        self._elements[element.id] = element

    def register_elements(self, elements: list["UIElement"]) -> None:
        """Register multiple elements without clearing the full registry."""
        if self._registered_at == 0:
            self._registered_at = time.time()
        for el in elements:
            self._elements[el.id] = el

    def count(self) -> int:
        return len(self._elements)

    def find(self, role: str = "", name: str = "", value: str = "") -> list[UIElement]:
        """Search registered elements by criteria."""
        if not role and not name and not value:
            return []
        role_q = role.lower() if role else ""
        name_q = name.lower() if name else ""
        value_q = value.lower() if value else ""
        results = []
        for el in self._elements.values():
            if role_q and role_q not in el.role.lower():
                continue
            if name_q and name_q not in el.name.lower():
                continue
            if value_q and value_q not in el.value.lower():
                continue
            results.append(el)
        return results
