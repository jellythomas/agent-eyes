"""Tests for Task 3: New Response Format — Flat Text, One-Liners."""
from __future__ import annotations

import pytest
from agent_eyes.adapters.base import UIElement, INTERACTIVE_ROLES


class TestInteractiveRoles:
    """INTERACTIVE_ROLES frozenset exists and contains expected roles."""

    def test_is_frozenset(self):
        assert isinstance(INTERACTIVE_ROLES, frozenset)

    def test_contains_button(self):
        assert "button" in INTERACTIVE_ROLES

    def test_contains_link(self):
        assert "link" in INTERACTIVE_ROLES

    def test_contains_textbox(self):
        assert "textbox" in INTERACTIVE_ROLES

    def test_contains_checkbox(self):
        assert "checkbox" in INTERACTIVE_ROLES

    def test_contains_radio(self):
        assert "radio" in INTERACTIVE_ROLES

    def test_contains_combobox(self):
        assert "combobox" in INTERACTIVE_ROLES

    def test_contains_select(self):
        assert "select" in INTERACTIVE_ROLES

    def test_contains_tab(self):
        assert "tab" in INTERACTIVE_ROLES

    def test_contains_menuitem(self):
        assert "menuitem" in INTERACTIVE_ROLES

    def test_contains_slider(self):
        assert "slider" in INTERACTIVE_ROLES

    def test_contains_switch(self):
        assert "switch" in INTERACTIVE_ROLES

    def test_contains_searchbox(self):
        assert "searchbox" in INTERACTIVE_ROLES

    def test_contains_spinbutton(self):
        assert "spinbutton" in INTERACTIVE_ROLES

    def test_contains_textarea(self):
        assert "textarea" in INTERACTIVE_ROLES

    def test_does_not_contain_heading(self):
        assert "heading" not in INTERACTIVE_ROLES

    def test_does_not_contain_text(self):
        assert "text" not in INTERACTIVE_ROLES

    def test_does_not_contain_group(self):
        assert "group" not in INTERACTIVE_ROLES


class TestToFlatLine:
    """UIElement.to_flat_line() renders as compact single-line string."""

    def test_id_and_role_only(self):
        el = UIElement(id=1, role="button")
        assert el.to_flat_line() == '[1] button'

    def test_includes_name(self):
        el = UIElement(id=2, role="link", name="About")
        assert el.to_flat_line() == '[2] link "About"'

    def test_includes_value(self):
        el = UIElement(id=3, role="textbox", name="Search", value="hello")
        assert el.to_flat_line() == '[3] textbox "Search" value="hello"'

    def test_includes_meaningful_states(self):
        el = UIElement(id=4, role="textbox", name="Email", states=["focused"])
        assert el.to_flat_line() == '[4] textbox "Email" focused'

    def test_skips_enabled_state(self):
        el = UIElement(id=5, role="button", name="Submit", states=["enabled"])
        assert el.to_flat_line() == '[5] button "Submit"'

    def test_skips_enabled_keeps_others(self):
        el = UIElement(id=6, role="checkbox", name="Remember me", states=["enabled", "checked"])
        result = el.to_flat_line()
        assert "checked" in result
        assert "enabled" not in result

    def test_multiple_states(self):
        el = UIElement(id=7, role="textbox", name="Query", states=["focused", "required"])
        result = el.to_flat_line()
        assert "focused" in result
        assert "required" in result

    def test_no_name_no_value(self):
        el = UIElement(id=8, role="menuitem")
        assert el.to_flat_line() == '[8] menuitem'

    def test_value_without_name(self):
        el = UIElement(id=9, role="textbox", value="typed text")
        assert el.to_flat_line() == '[9] textbox value="typed text"'

    def test_returns_single_line(self):
        el = UIElement(id=10, role="button", name="OK", states=["focused", "enabled"])
        result = el.to_flat_line()
        assert "\n" not in result

    def test_format_starts_with_bracketed_id(self):
        el = UIElement(id=42, role="slider", name="Volume")
        result = el.to_flat_line()
        assert result.startswith("[42]")

    def test_empty_name_not_included(self):
        el = UIElement(id=11, role="button", name="")
        result = el.to_flat_line()
        assert '"' not in result

    def test_empty_value_not_included(self):
        el = UIElement(id=12, role="textbox", value="")
        result = el.to_flat_line()
        assert "value=" not in result
