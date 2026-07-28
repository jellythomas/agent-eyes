"""Pure, transaction-local indexing and matching for accessibility elements."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .adapters.base import UIElement
from .operation import OperationError, OperationErrorCode
from .transaction_contract import Locator, MatchMode


def match_text(query: str, text: str, mode: MatchMode) -> bool:
    """Match text with the case-insensitive semantics used by the public find tool."""
    if not query:
        return True

    normalized_query = query.lower()
    normalized_text = text.lower()
    if mode is MatchMode.EXACT:
        return normalized_text == normalized_query
    if mode is MatchMode.PREFIX:
        return normalized_text.startswith(normalized_query)
    if mode is MatchMode.SUFFIX:
        return normalized_text.endswith(normalized_query)
    if mode is MatchMode.CONTAINS:
        return normalized_query in normalized_text
    raise ValueError("unsupported locator match mode")


def _matches(locator: Locator, element: UIElement) -> bool:
    return (
        match_text(locator.role, element.role, locator.match)
        and match_text(locator.name, element.name, locator.match)
        and match_text(locator.value, element.value, locator.match)
    )


class LocatorIndex:
    """Immutable DFS index for one bounded accessibility observation.

    Element identity, rather than registry IDs or dataclass equality, binds aliases
    to their observed tree. A new provider observation must create a new index.
    """

    def __init__(self, roots: Iterable[UIElement]) -> None:
        elements: list[UIElement] = []
        positions: dict[int, int] = {}
        parents: dict[int, UIElement | None] = {}
        children: dict[int, list[UIElement]] = {}
        subtree_ends: dict[int, int] = {}

        def visit(element: UIElement, parent: UIElement | None) -> None:
            identity = id(element)
            if identity in positions:
                return

            positions[identity] = len(elements)
            parents[identity] = parent
            children[identity] = []
            elements.append(element)
            if parent is not None:
                children[id(parent)].append(element)

            for child in tuple(element.children):
                visit(child, element)
            subtree_ends[identity] = len(elements)

        for root in tuple(roots):
            visit(root, None)

        self._elements = tuple(elements)
        self._positions = positions
        self._parents = parents
        self._children = {
            identity: tuple(indexed_children)
            for identity, indexed_children in children.items()
        }
        self._subtree_ends = subtree_ends

    @classmethod
    def from_roots(
        cls,
        roots: UIElement | Iterable[UIElement],
    ) -> LocatorIndex:
        """Build one frozen index from a root or an ordered collection of roots."""
        if isinstance(roots, UIElement):
            return cls((roots,))
        return cls(roots)

    @property
    def elements(self) -> tuple[UIElement, ...]:
        """Return elements in deterministic root-first depth-first order."""
        return self._elements

    def parent_of(self, element: UIElement) -> UIElement | None:
        """Return the indexed parent, or ``None`` for an indexed root."""
        self._require_indexed(element)
        return self._parents[id(element)]

    def children_of(self, element: UIElement) -> tuple[UIElement, ...]:
        """Return the indexed children captured when the observation was frozen."""
        self._require_indexed(element)
        return self._children[id(element)]

    def find(
        self,
        locator: Locator,
        *,
        aliases: Mapping[str, UIElement] | None = None,
    ) -> tuple[UIElement, ...]:
        """Return every match in stable DFS order from the frozen index."""
        return self.find_many((locator,), aliases=aliases)[0]

    def find_many(
        self,
        locators: Sequence[Locator],
        *,
        aliases: Mapping[str, UIElement] | None = None,
    ) -> tuple[tuple[UIElement, ...], ...]:
        """Evaluate ordered selectors together without walking the source trees again."""
        if not locators:
            return ()

        alias_table = aliases or {}
        ranges = tuple(
            self._locator_range(locator, alias_table) for locator in locators
        )
        matches: list[list[UIElement]] = [[] for _ in locators]

        for position, element in enumerate(self._elements):
            for index, locator in enumerate(locators):
                start, end = ranges[index]
                if start <= position < end and _matches(locator, element):
                    matches[index].append(element)

        return tuple(tuple(result) for result in matches)

    def resolve_unique(
        self,
        locator: Locator,
        *,
        aliases: Mapping[str, UIElement] | None = None,
    ) -> UIElement:
        """Resolve exactly one element, failing closed for zero or multiple matches."""
        matches = self.find(locator, aliases=aliases)
        if not matches:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "locator matched no elements in the current observation",
            )
        if len(matches) != 1:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_ELEMENT,
                "locator matched multiple elements in the current observation",
            )
        return matches[0]

    def _locator_range(
        self,
        locator: Locator,
        aliases: Mapping[str, UIElement],
    ) -> tuple[int, int]:
        if not locator.within:
            return 0, len(self._elements)

        scope = aliases.get(locator.within)
        if scope is None or id(scope) not in self._positions:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "within scope is unavailable from the current observation",
            )
        identity = id(scope)
        return self._positions[identity] + 1, self._subtree_ends[identity]

    def _require_indexed(self, element: UIElement) -> None:
        if id(element) not in self._positions:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "element is unavailable from the current observation",
            )
