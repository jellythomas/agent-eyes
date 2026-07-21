from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_eyes.adapters.linux import LinuxAdapter


def _make_linux_adapter():
    state_type = SimpleNamespace(
        FOCUSED="focused",
        SELECTED="selected",
        ENABLED="enabled",
        VISIBLE="visible",
        ACTIVE="active",
    )
    atspi = SimpleNamespace(
        Role=SimpleNamespace(PASSWORD_TEXT="password-role"),
        StateType=state_type,
        CoordType=SimpleNamespace(SCREEN="screen"),
    )
    adapter = LinuxAdapter()
    adapter._atspi = atspi
    adapter.reset_ids()
    return adapter


def _make_atspi_element(*, role, role_name, value):
    element = MagicMock()
    element.get_role.return_value = role
    element.get_role_name.return_value = role_name
    element.get_name.return_value = "Password" if role_name == "password text" else "Search"
    element.get_description.return_value = ""

    value_interface = MagicMock()
    value_interface.get_current_value.return_value = value
    element.get_value.return_value = value_interface

    text_interface = MagicMock()
    text_interface.get_character_count.return_value = len(str(value))
    text_interface.get_text.return_value = str(value)
    element.get_text.return_value = text_interface

    state_set = MagicMock()
    state_set.contains.side_effect = lambda state: state in {"enabled", "visible"}
    element.get_state_set.return_value = state_set
    element.get_action.return_value = None
    element.get_component.return_value = None
    element.get_child_count.return_value = 0
    return element


def test_password_text_value_interfaces_are_never_queried():
    secret = "linux-password-secret-90cf"
    adapter = _make_linux_adapter()
    element = _make_atspi_element(
        role=adapter._atspi.Role.PASSWORD_TEXT,
        role_name="password text",
        value=secret,
    )

    ui = adapter._atspi_to_ui(element, 0, 0)

    assert ui is not None
    assert ui.value == ""
    assert "secure" in ui.states
    assert secret not in ui.to_text()
    element.get_value.assert_not_called()
    element.get_text.assert_not_called()


def test_password_role_name_does_not_need_enum_lookup():
    secret = "linux-fallback-secret-1c07"
    adapter = _make_linux_adapter()
    element = _make_atspi_element(
        role=adapter._atspi.Role.PASSWORD_TEXT,
        role_name="password text",
        value=secret,
    )
    element.get_role.side_effect = RuntimeError("provider unavailable")

    ui = adapter._atspi_to_ui(element, 0, 0)

    assert ui is not None
    assert ui.value == ""
    assert "secure" in ui.states
    element.get_role.assert_not_called()
    element.get_value.assert_not_called()
    element.get_text.assert_not_called()


def test_non_secure_entry_value_is_preserved():
    adapter = _make_linux_adapter()
    element = _make_atspi_element(
        role="entry-role",
        role_name="entry",
        value=42.5,
    )

    ui = adapter._atspi_to_ui(element, 0, 0)

    assert ui is not None
    assert ui.value == "42.5"
    assert "secure" not in ui.states
    element.get_role.assert_not_called()
    element.get_value.assert_called_once_with()


def test_unknown_role_name_uses_password_enum_fallback():
    secret = "linux-enum-fallback-secret-704b"
    adapter = _make_linux_adapter()
    element = _make_atspi_element(
        role=adapter._atspi.Role.PASSWORD_TEXT,
        role_name="unknown",
        value=secret,
    )

    ui = adapter._atspi_to_ui(element, 0, 0)

    assert ui is not None
    assert ui.value == ""
    assert "secure" in ui.states
    element.get_role.assert_called_once_with()
    element.get_value.assert_not_called()


def test_native_identity_uses_atspi_reference_equality():
    from agent_eyes.adapters.base import UIElement

    first_ref = object()
    second_ref = object()
    adapter = _make_linux_adapter()
    first = UIElement(id=1, role="entry", platform_ref=first_ref)
    same = UIElement(id=2, role="entry", platform_ref=first_ref)
    different = UIElement(id=3, role="entry", platform_ref=second_ref)

    assert adapter.is_same_element(first, same) is True
    assert adapter.is_same_element(first, different) is False
