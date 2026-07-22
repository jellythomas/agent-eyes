from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.cdp import CDPMutationOutcomeUnknown


def test_new_tab_reuses_matching_foreground_tab_without_cdp_or_open(monkeypatch):
    from agent_eyes import server

    target = BrowserTarget(
        browser="Firefox",
        pid=51,
        title="Agent Eyes documentation",
        element=UIElement(id=3, role="tab", name="Agent Eyes documentation"),
    )
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [target])
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", AsyncMock(return_value=True))
    monkeypatch.setattr(server._pu, "open_url_in_browser", MagicMock())
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock())

    result = asyncio.run(
        server._handle_new_tab(
            {
                "url": "https://example.com/docs",
                "query": "agent eyes documentation",
            }
        )
    )

    assert "Reused" in result
    assert server._activate_browser_target_and_wait.await_count == 1
    assert server._activate_browser_target_and_wait.await_args.args[0].pid == target.pid
    server._pu.open_url_in_browser.assert_not_called()
    server.cdp_client.is_available.assert_not_awaited()


def test_new_tab_opens_system_default_only_after_no_native_match(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(
        server._pu,
        "open_url_in_browser",
        MagicMock(return_value=(True, "Opened in default browser")),
    )
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock())

    result = asyncio.run(
        server._handle_new_tab(
            {"url": "https://example.com", "query": "example project"}
        )
    )

    assert "default browser" in result
    server._pu.open_url_in_browser.assert_called_once_with("https://example.com")
    server.cdp_client.is_available.assert_not_awaited()


def test_failed_reuse_does_not_open_a_duplicate(monkeypatch):
    from agent_eyes import server

    target = BrowserTarget(
        browser="Safari",
        pid=12,
        title="Agent Eyes documentation",
        element=UIElement(id=1, role="tab", name="Agent Eyes documentation"),
    )
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [target])
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", AsyncMock(return_value=False))
    monkeypatch.setattr(server._pu, "open_url_in_browser", MagicMock())

    result = asyncio.run(
        server._handle_new_tab(
            {"url": "https://example.com", "query": "agent eyes documentation"}
        )
    )

    assert "already-open match" in result
    server._pu.open_url_in_browser.assert_not_called()


def test_explicit_shadow_new_tab_may_use_cdp(monkeypatch):
    from agent_eyes import server

    tab = MagicMock(title="Background", url="https://example.com")
    monkeypatch.setattr(server, "_cached_tabs", [])
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(server.cdp_client, "list_tabs", AsyncMock(return_value=[]))
    monkeypatch.setattr(server.cdp_client, "new_tab", AsyncMock(return_value=tab))

    result = asyncio.run(
        server._handle_new_tab({"url": "https://example.com", "shadow": True})
    )

    assert "shadow" in result.lower()
    server.cdp_client.new_tab.assert_awaited_once_with("https://example.com")


def test_shadow_new_tab_reports_unknown_outcome_after_dispatch_failure(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_cached_tabs", [])
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(server.cdp_client, "list_tabs", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server.cdp_client,
        "new_tab",
        AsyncMock(side_effect=CDPMutationOutcomeUnknown("response lost")),
    )

    result = asyncio.run(
        server._handle_new_tab({"url": "https://example.com", "shadow": True})
    )

    assert "OUTCOME_UNKNOWN" in result


def test_new_tab_rejects_unsafe_url_before_any_provider_probe(monkeypatch):
    from agent_eyes import server

    inventory = MagicMock(side_effect=AssertionError("inventory was probed"))
    opener = MagicMock(side_effect=AssertionError("browser was opened"))
    monkeypatch.setattr(server, "collect_browser_targets", inventory)
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(
        server.cdp_client,
        "is_available",
        AsyncMock(side_effect=AssertionError("shadow provider was probed")),
    )

    result = asyncio.run(
        server._handle_new_tab({"url": "file:///private/etc/passwd"})
    )

    assert "not permitted" in result
    inventory.assert_not_called()
    opener.assert_not_called()
    server.cdp_client.is_available.assert_not_awaited()
