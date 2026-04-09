"""Unified Element Registry — maps integer IDs to live platform references.

This is the bridge between the text representation sent to Claude
and the actual native/CDP elements that can be acted upon.

Elements expire when the page changes (URL mismatch), not on a timer.
"""
from __future__ import annotations

from .adapters.base import UIElement


_MAX_ELEMENTS = 500


class ElementRegistry:
    """Maps element IDs to their UIElement objects for action dispatch."""

    def __init__(self):
        self._elements: dict[int, UIElement] = {}
        self.last_pid: int = 0
        self.last_tab_index: int = -1
        self._page_url: str = ""

    def clear(self):
        self._elements.clear()
        self._page_url = ""

    def register_tree(
        self,
        root: UIElement,
        pid: int = 0,
        tab_index: int = -1,
        *,
        page_url: str = "",
    ):
        """Walk a tree and register all elements, tagging each with the owning PID and tab_index."""
        self.clear()
        self.last_pid = pid
        self.last_tab_index = tab_index
        self._page_url = page_url
        self._walk_and_register(root, pid, tab_index)

    def _walk_and_register(self, element: UIElement, pid: int = 0, tab_index: int = -1):
        if len(self._elements) >= _MAX_ELEMENTS:
            return
        if pid:
            element.pid = pid
        if tab_index >= 0:
            element.tab_index = tab_index
        self._elements[element.id] = element
        for child in element.children:
            if len(self._elements) >= _MAX_ELEMENTS:
                break
            self._walk_and_register(child, pid, tab_index)

    def is_valid_for(self, *, page_url: str = "") -> bool:
        """Return True if the registry is valid for the given page URL.

        When page_url is empty (native/desktop elements), always returns True.
        When _page_url is empty (registry was cleared or never populated with a URL),
        always returns True.
        """
        if not page_url or not self._page_url:
            return True
        return self._page_url == page_url

    def get(self, element_id: int) -> UIElement | None:
        return self._elements.get(element_id)

    def get_checked(
        self,
        element_id: int,
        *,
        current_page_url: str = "",
    ) -> tuple[UIElement | None, str]:
        """Get element with page-state validation.

        Returns (element, reason). On success, element is set and reason is "".
        On failure, element is None and reason describes why.
        """
        if not self.is_valid_for(page_url=current_page_url):
            return None, "page changed — call web_tree to refresh"

        element = self._elements.get(element_id)
        if element is None:
            return None, f"element [{element_id}] not found — call tree to refresh"

        return element, ""

    def register_element(self, element: UIElement) -> None:
        """Register a single element without clearing the full registry."""
        self._elements[element.id] = element

    def register_elements(self, elements: list[UIElement]) -> None:
        """Register multiple elements without clearing the full registry."""
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
