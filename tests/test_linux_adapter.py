from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_element_at_position_returns_deepest_provider_owned_atspi_element():
    from agent_eyes.adapters.base import UIElement

    adapter = _make_linux_adapter()
    desktop = MagicMock()
    window = MagicMock()
    button = _make_atspi_element(
        role="push-button-role",
        role_name="push button",
        value="",
    )

    desktop_component = MagicMock()
    window_component = MagicMock()
    button_component = MagicMock()
    desktop.get_component.return_value = desktop_component
    window.get_component.return_value = window_component
    button.get_component.return_value = button_component
    desktop_component.get_accessible_at_point.return_value = window
    window_component.get_accessible_at_point.return_value = button
    button_component.get_accessible_at_point.return_value = None
    button_component.get_extents.return_value = SimpleNamespace(
        x=20,
        y=30,
        width=40,
        height=20,
    )
    adapter._atspi.get_desktop = lambda _index: desktop

    result = adapter.element_at_position(40.9, 50.8)

    assert result is not None
    assert result.platform_ref is button
    assert adapter.is_same_element(
        result,
        UIElement(id=99, role="button", platform_ref=button),
    )
    desktop_component.get_accessible_at_point.assert_called_once_with(
        40,
        50,
        adapter._atspi.CoordType.SCREEN,
    )
    window_component.get_accessible_at_point.assert_called_once_with(
        40,
        50,
        adapter._atspi.CoordType.SCREEN,
    )
    button_component.get_accessible_at_point.assert_called_once_with(
        40,
        50,
        adapter._atspi.CoordType.SCREEN,
    )


def test_element_at_position_fails_closed_when_atspi_hit_test_is_unavailable():
    adapter = _make_linux_adapter()
    desktop = MagicMock()
    desktop.get_component.side_effect = RuntimeError("AT-SPI hit test unavailable")
    adapter._atspi.get_desktop = lambda _index: desktop

    assert adapter.element_at_position(40, 50) is None


def test_complete_app_inventory_propagates_partial_atspi_failure():
    adapter = _make_linux_adapter()
    broken = MagicMock()
    broken.get_name.side_effect = RuntimeError("AT-SPI app unavailable")
    healthy = MagicMock()
    healthy.get_name.return_value = "Firefox"
    healthy.get_process_id.return_value = 73
    healthy.get_child_count.return_value = 0
    desktop = MagicMock()
    desktop.get_child_count.return_value = 2
    desktop.get_child_at_index.side_effect = [broken, healthy, broken]
    adapter._atspi.get_desktop = lambda _index: desktop

    assert [app.name for app in adapter.list_apps()] == ["Firefox"]
    with pytest.raises(RuntimeError, match="AT-SPI app unavailable"):
        adapter.list_apps_complete()


def test_complete_browser_inventory_rejects_missing_atspi_process():
    adapter = _make_linux_adapter()
    desktop = MagicMock()
    desktop.get_child_count.return_value = 0
    adapter._atspi.get_desktop = lambda _index: desktop

    assert adapter.get_browser_trees(73) == []
    with pytest.raises(RuntimeError, match="browser process"):
        adapter.get_browser_trees_complete(73)


def _set_atspi_children(element, children):
    element.get_child_count.return_value = len(children)
    element.get_child_at_index.side_effect = lambda index: children[index]


def _configure_linux_browser(adapter, window, *, pid=73):
    app = MagicMock()
    app.get_process_id.return_value = pid
    _set_atspi_children(app, [window])
    desktop = MagicMock()
    _set_atspi_children(desktop, [app])
    adapter._atspi.get_desktop = lambda _index: desktop


def test_complete_browser_inventory_rejects_traversal_cap_truncation():
    adapter = _make_linux_adapter()
    first = _make_atspi_element(role="page-tab", role_name="page tab", value="")
    second = _make_atspi_element(role="page-tab", role_name="page tab", value="")
    window = _make_atspi_element(role="frame", role_name="frame", value="")
    _set_atspi_children(window, [first, second])
    _configure_linux_browser(adapter, window)
    adapter._MAX_ELEMENTS = 2

    assert len(adapter.get_browser_trees(73)[0].children) == 1
    with pytest.raises(RuntimeError, match="traversal safety cap"):
        adapter.get_browser_trees_complete(73)


def test_complete_browser_inventory_rejects_depth_truncation():
    adapter = _make_linux_adapter()
    tab = _make_atspi_element(role="page-tab", role_name="page tab", value="")
    group = _make_atspi_element(role="panel", role_name="panel", value="")
    _set_atspi_children(group, [tab])
    window = _make_atspi_element(role="frame", role_name="frame", value="")
    _set_atspi_children(window, [group])
    _configure_linux_browser(adapter, window)

    assert adapter.get_browser_trees(73, max_depth=1)[0].children[0].children == []
    with pytest.raises(RuntimeError, match="traversal depth bound"):
        adapter.get_browser_trees_complete(73, max_depth=1)


def test_complete_browser_inventory_propagates_child_read_failure():
    adapter = _make_linux_adapter()
    broken = MagicMock()
    broken.get_role_name.side_effect = RuntimeError("broken AT-SPI child")
    window = _make_atspi_element(role="frame", role_name="frame", value="")
    _set_atspi_children(window, [broken])
    _configure_linux_browser(adapter, window)

    assert adapter.get_browser_trees(73)[0].children == []
    with pytest.raises(RuntimeError, match="broken AT-SPI child"):
        adapter.get_browser_trees_complete(73)
