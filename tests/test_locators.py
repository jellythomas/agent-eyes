from __future__ import annotations

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.locators import LocatorIndex, match_text
from agent_eyes.operation import OperationError, OperationErrorCode
from agent_eyes.transaction_contract import Locator, MatchMode


def _tree() -> tuple[UIElement, dict[str, UIElement]]:
    first_save = UIElement(id=30, role="button", name="Save comment")
    first_editor = UIElement(id=31, role="textbox", name="Comment editor")
    first_row = UIElement(
        id=20,
        role="group",
        name="First discussion",
        children=[first_save, first_editor],
    )
    second_save = UIElement(id=10, role="button", name="Save comment")
    second_row = UIElement(
        id=40,
        role="group",
        name="Second discussion",
        children=[second_save],
    )
    root = UIElement(
        id=99,
        role="window",
        name="Pull request",
        children=[first_row, second_row],
    )
    return root, {
        "root": root,
        "first_row": first_row,
        "first_save": first_save,
        "first_editor": first_editor,
        "second_row": second_row,
        "second_save": second_save,
    }


@pytest.mark.parametrize(
    ("mode", "query", "text", "expected"),
    [
        (MatchMode.EXACT, "SAVE COMMENT", "Save Comment", True),
        (MatchMode.EXACT, "Save", "Save Comment", False),
        (MatchMode.CONTAINS, "ve com", "Save Comment", True),
        (MatchMode.CONTAINS, "missing", "Save Comment", False),
        (MatchMode.PREFIX, "SAVE", "Save Comment", True),
        (MatchMode.PREFIX, "comment", "Save Comment", False),
        (MatchMode.SUFFIX, "COMMENT", "Save Comment", True),
        (MatchMode.SUFFIX, "save", "Save Comment", False),
        (MatchMode.EXACT, "", "anything", True),
    ],
)
def test_match_text_preserves_case_insensitive_find_semantics(
    mode: MatchMode,
    query: str,
    text: str,
    expected: bool,
) -> None:
    assert match_text(query, text, mode) is expected


def test_find_uses_conjunctive_fields_and_deterministic_depth_first_order() -> None:
    root, elements = _tree()
    index = LocatorIndex.from_roots(root)

    matches = index.find(Locator(role="BUTTON", name="save", match=MatchMode.PREFIX))

    assert matches == (elements["first_save"], elements["second_save"])
    assert index.elements == (
        elements["root"],
        elements["first_row"],
        elements["first_save"],
        elements["first_editor"],
        elements["second_row"],
        elements["second_save"],
    )


def test_find_matches_accessible_values_with_the_selected_mode() -> None:
    field = UIElement(
        id=1,
        role="textbox",
        name="Status",
        value="Ready for review",
    )
    index = LocatorIndex.from_roots(field)

    assert index.find(Locator(value="FOR REVIEW", match=MatchMode.SUFFIX)) == (field,)
    assert index.find(Locator(value="review", match=MatchMode.EXACT)) == ()


def test_multiple_roots_preserve_root_and_child_insertion_order() -> None:
    first = UIElement(
        id=90,
        role="group",
        children=[UIElement(id=1, role="button", name="First")],
    )
    second = UIElement(
        id=2,
        role="group",
        children=[UIElement(id=80, role="button", name="Second")],
    )

    index = LocatorIndex.from_roots([first, second])

    assert [element.name for element in index.find(Locator(role="button"))] == [
        "First",
        "Second",
    ]


def test_within_scope_searches_strict_descendants_of_injected_alias() -> None:
    root, elements = _tree()
    index = LocatorIndex.from_roots(root)
    aliases = {"discussion": elements["second_row"]}

    matches = index.find(
        Locator(
            role="button",
            name="save comment",
            match=MatchMode.EXACT,
            within="discussion",
        ),
        aliases=aliases,
    )
    scope_itself = index.find(
        Locator(role="group", within="discussion"),
        aliases=aliases,
    )

    assert matches == (elements["second_save"],)
    assert scope_itself == ()
    assert index.parent_of(elements["second_save"]) is elements["second_row"]
    assert index.children_of(elements["second_row"]) == (elements["second_save"],)


@pytest.mark.parametrize("aliases", [{}, {"discussion": UIElement(id=1, role="group")}])
def test_unknown_or_foreign_within_scope_fails_closed(
    aliases: dict[str, UIElement],
) -> None:
    root, _ = _tree()
    index = LocatorIndex.from_roots(root)

    with pytest.raises(OperationError) as exc_info:
        index.find(
            Locator(role="button", within="discussion"),
            aliases=aliases,
        )

    assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND


def test_resolve_unique_distinguishes_missing_and_ambiguous_elements() -> None:
    root, elements = _tree()
    index = LocatorIndex.from_roots(root)

    assert (
        index.resolve_unique(Locator(role="textbox", name="comment editor"))
        is elements["first_editor"]
    )

    with pytest.raises(OperationError) as missing:
        index.resolve_unique(Locator(role="button", name="Publish"))
    with pytest.raises(OperationError) as ambiguous:
        index.resolve_unique(
            Locator(role="button", name="Save", match=MatchMode.PREFIX)
        )

    assert missing.value.code is OperationErrorCode.ELEMENT_NOT_FOUND
    assert ambiguous.value.code is OperationErrorCode.AMBIGUOUS_ELEMENT
    assert "Publish" not in str(missing.value)
    assert "Save" not in str(ambiguous.value)


def test_find_many_evaluates_all_selectors_from_the_frozen_index_once() -> None:
    root, elements = _tree()
    index = LocatorIndex.from_roots(root)
    selectors = (
        Locator(role="button", name="save", match=MatchMode.PREFIX),
        Locator(role="textbox"),
        Locator(role="button", within="discussion"),
    )

    # Mutating the provider-shaped tree after indexing proves batch lookup does
    # not walk or rescan it. Transaction refreshes create a new LocatorIndex.
    root.children.clear()
    elements["first_row"].children.clear()
    elements["second_row"].children.clear()

    matches = index.find_many(
        selectors,
        aliases={"discussion": elements["second_row"]},
    )

    assert matches == (
        (elements["first_save"], elements["second_save"]),
        (elements["first_editor"],),
        (elements["second_save"],),
    )


def test_repeated_element_identity_is_indexed_at_its_first_dfs_position() -> None:
    shared = UIElement(id=7, role="button", name="Shared")
    first = UIElement(id=1, role="group", children=[shared])
    second = UIElement(id=2, role="group", children=[shared])
    root = UIElement(id=0, role="window", children=[first, second])

    index = LocatorIndex.from_roots(root)

    assert index.find(Locator(role="button")) == (shared,)
    assert index.parent_of(shared) is first
