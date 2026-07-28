from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator


def test_foreground_close_rejects_positional_index_before_inventory(monkeypatch):
    from agent_eyes import server

    inventory = MagicMock(side_effect=AssertionError("inventory must not be queried"))
    monkeypatch.setattr(server, "collect_browser_targets", inventory)

    result = asyncio.run(server._handle_close_tab({"tab_index": 0}))

    assert "does not accept positional tab_index" in result
    inventory.assert_not_called()


def test_foreground_close_requires_stable_target_or_unique_title(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "native_adapter", object())
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "collect_browser_targets", lambda _adapter: [])

    result = asyncio.run(server._handle_close_tab({}))

    assert result == "ERROR: Provide target_id or a unique title from a fresh list_tabs result."


def test_foreground_close_rejects_ambiguous_title_without_activation(monkeypatch):
    from agent_eyes import server

    targets = [
        BrowserTarget(browser="Firefox", pid=10, title="YouTube - Home"),
        BrowserTarget(browser="Safari", pid=20, title="YouTube Music"),
    ]
    activate = AsyncMock(side_effect=AssertionError("ambiguous target must not activate"))
    monkeypatch.setattr(server, "native_adapter", object())
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "collect_browser_targets", lambda _adapter: targets)
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", activate)

    result = asyncio.run(server._handle_close_tab({"title": "youtube"}))

    assert "2 foreground tabs matched" in result
    assert "use target_id" in result
    activate.assert_not_awaited()


def test_foreground_close_invalidates_native_state_after_confirmed_shortcut(monkeypatch):
    from agent_eyes import server

    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Exact target",
        element=UIElement(id=1, role="tab", platform_ref=object()),
        window_element=UIElement(id=2, role="window", platform_ref=object()),
    )
    backend = MagicMock()
    backend.is_available.return_value = True
    backend.hotkey.return_value = True
    invalidate = MagicMock()
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.is_window_focused.return_value = True
    adapter.is_element_selected.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "_native_target_cache", {target.identifier: target})
    monkeypatch.setattr(
        server,
        "_activate_browser_target_and_wait",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", invalidate)

    result = asyncio.run(server._handle_close_tab({"target_id": target.identifier}))

    assert result == "Closed foreground Firefox tab."
    invalidate.assert_called_once_with(pid=42)


def test_foreground_close_rejects_window_only_target_without_shortcut(monkeypatch):
    from agent_eyes import server

    window = UIElement(id=2, role="window", platform_ref=object())
    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Window only",
        source="native-window",
        element=window,
        window_element=window,
    )
    backend = MagicMock()
    monkeypatch.setattr(server, "native_adapter", MagicMock())
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "_native_target_cache", {target.identifier: target})

    result = asyncio.run(server._handle_close_tab({"target_id": target.identifier}))

    assert "UNSUPPORTED_CAPABILITY" in result
    backend.hotkey.assert_not_called()


def test_foreground_close_rechecks_exact_window_and_tab_before_shortcut(monkeypatch):
    from agent_eyes import server

    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Exact target",
        element=UIElement(id=1, role="tab", platform_ref=object()),
        window_element=UIElement(id=2, role="window", platform_ref=object()),
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.is_window_focused.return_value = False
    adapter.is_element_selected.return_value = True
    backend = MagicMock()
    backend.is_available.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "_native_target_cache", {target.identifier: target})
    monkeypatch.setattr(
        server,
        "_activate_browser_target_and_wait",
        AsyncMock(return_value=True),
    )

    result = asyncio.run(server._handle_close_tab({"target_id": target.identifier}))

    assert "FOCUS_MISMATCH" in result
    backend.hotkey.assert_not_called()


def test_shadow_close_requires_exact_target_before_provider_probe(monkeypatch):
    from agent_eyes import server

    provider = AsyncMock(side_effect=AssertionError("provider must not be queried"))
    monkeypatch.setattr(server, "_get_cdp_session", provider)

    result = asyncio.run(server._handle_close_tab({"shadow": True}))

    assert "requires target_id" in result
    provider.assert_not_awaited()


def test_shadow_close_success_invalidates_target_and_removes_cached_tab(monkeypatch):
    from agent_eyes import server

    target_id = "target-b"
    session = MagicMock()
    tab_a = MagicMock(id="target-a")
    tab_b = MagicMock(id=target_id)
    invalidate = MagicMock()
    apple_cache = MagicMock()
    pool = MagicMock()
    pool.close_target = AsyncMock(return_value=True)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab_b, "")),
    )
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(server, "_cached_tabs", [tab_a, tab_b])
    monkeypatch.setattr(server, "_invalidate_shadow_observation", invalidate)
    monkeypatch.setattr(server, "_invalidate_applescript_tab_cache", apple_cache)

    result = asyncio.run(
        server._handle_close_tab({"shadow": True, "target_id": target_id})
    )

    assert result == "Closed the requested shadow target."
    pool.close_target.assert_awaited_once_with(target_id)
    invalidate.assert_called_once_with("cdp-persistent", target_id)
    assert server._cached_tabs == [tab_a]
    apple_cache.assert_called_once_with()


def test_shadow_close_unconfirmed_outcome_invalidates_and_clears_cache(monkeypatch):
    from agent_eyes import server

    target_id = "target-b"
    tab = MagicMock(id=target_id)
    invalidate = MagicMock()
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(None, tab, "")),
    )
    monkeypatch.setattr(server.cdp_client, "close_tab", AsyncMock(return_value=False))
    monkeypatch.setattr(server, "_cached_tabs", [tab])
    monkeypatch.setattr(server, "_invalidate_shadow_observation", invalidate)

    result = asyncio.run(
        server._handle_close_tab({"shadow": True, "target_id": target_id})
    )

    assert "OUTCOME_UNKNOWN" in result
    invalidate.assert_called_once_with("cdp-legacy", target_id)
    assert server._cached_tabs == []
