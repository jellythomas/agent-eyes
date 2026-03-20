"""Unified Element Registry — maps integer IDs to live platform references.

This is the bridge between the text representation sent to Claude
and the actual native/CDP elements that can be acted upon.
"""
from __future__ import annotations

from .adapters.base import UIElement


class ElementRegistry:
    """Maps element IDs to their UIElement objects for action dispatch."""

    def __init__(self):
        self._elements: dict[int, UIElement] = {}
        self.last_pid: int = 0  # PID from the last get_tree call

    def clear(self):
        self._elements.clear()

    def register_tree(self, root: UIElement, pid: int = 0):
        """Walk a tree and register all elements, tagging each with the owning PID."""
        self.clear()
        self.last_pid = pid
        self._walk_and_register(root, pid)

    def _walk_and_register(self, element: UIElement, pid: int = 0):
        if pid:
            element.pid = pid
        self._elements[element.id] = element
        for child in element.children:
            self._walk_and_register(child, pid)

    def get(self, element_id: int) -> UIElement | None:
        return self._elements.get(element_id)

    def count(self) -> int:
        return len(self._elements)

    def find(self, role: str = "", name: str = "", value: str = "") -> list[UIElement]:
        """Search registered elements by criteria."""
        results = []
        for el in self._elements.values():
            match = True
            if role and role.lower() not in el.role.lower():
                match = False
            if name and name.lower() not in el.name.lower():
                match = False
            if value and value.lower() not in el.value.lower():
                match = False
            if match and (role or name or value):
                results.append(el)
        return results
