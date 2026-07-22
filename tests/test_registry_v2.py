"""Registry invalidation based on page state, not TTL timer."""
from agent_eyes.registry import ElementRegistry
from agent_eyes.adapters.base import UIElement


def test_registry_invalidates_on_navigation():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Old")
    reg.register_tree(tree, page_url="https://old.com")
    assert reg.is_valid_for(page_url="https://old.com")
    assert not reg.is_valid_for(page_url="https://new.com")


def test_registry_valid_same_page():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Click")
    reg.register_tree(tree, page_url="https://example.com")
    el = reg.get(1)
    assert el is not None
    assert el.name == "Click"


def test_registry_stale_returns_clear_error():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Old")
    reg.register_tree(tree, page_url="https://old.com")
    el, reason = reg.get_checked(1, current_page_url="https://new.com")
    assert el is None
    assert "page changed" in reason


def test_registry_cap_at_500():
    reg = ElementRegistry()
    children = [UIElement(id=i, role="button", name=f"B{i}") for i in range(1, 601)]
    root = UIElement(id=0, role="WebArea", name="Page", children=children)
    reg.register_tree(root, page_url="https://example.com")
    assert reg.count() <= 500


def test_incremental_registration_remains_bounded_and_keeps_newest():
    reg = ElementRegistry()

    for element_id in range(750):
        reg.register_element(UIElement(id=element_id, role="button"))

    assert reg.count() == 500
    assert reg.get(749) is not None
    assert reg.get(0) is None


def test_bulk_incremental_registration_remains_bounded():
    reg = ElementRegistry()

    reg.register_elements(
        [UIElement(id=element_id, role="button") for element_id in range(750)]
    )

    assert reg.count() == 500


def test_registry_get_checked_element_not_found():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Only")
    reg.register_tree(tree, page_url="https://example.com")
    el, reason = reg.get_checked(999, current_page_url="https://example.com")
    assert el is None
    assert "not found" in reason


def test_registry_get_checked_success():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Click")
    reg.register_tree(tree, page_url="https://example.com")
    el, reason = reg.get_checked(1, current_page_url="https://example.com")
    assert el is not None
    assert reason == ""


def test_registry_clear_resets_page_url():
    reg = ElementRegistry()
    tree = UIElement(id=1, role="button", name="Old")
    reg.register_tree(tree, page_url="https://old.com")
    reg.clear()
    assert reg.count() == 0
    # After clear, no page_url set, so is_valid_for should return True
    assert reg.is_valid_for(page_url="https://anything.com")
