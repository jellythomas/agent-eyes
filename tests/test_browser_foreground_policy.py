from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.input_sim import MacOSInputBackend


def _unexpected_shadow_probe(*args, **kwargs):
    raise AssertionError("foreground operation probed the shadow provider")


def test_macos_activation_uses_exact_pid_without_fixed_delay(monkeypatch):
    def unexpected_sleep(*args, **kwargs):
        raise AssertionError("activation used a fixed delay")

    monkeypatch.setattr("agent_eyes.input_sim.time.sleep", unexpected_sleep)
    runner = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("agent_eyes.input_sim.subprocess.run", runner)

    assert MacOSInputBackend().activate_window(42) is True
    runner.assert_called_once_with(
        [
            "/usr/bin/osascript",
            "-e",
            "tell application \"System Events\" to set frontmost of "
            "(first process whose unix id is 42) to true",
        ],
        capture_output=True,
        timeout=3,
    )


def test_browser_pid_press_key_uses_foreground_os_input(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    backend.activate_window.return_value = True
    backend.is_frontmost.return_value = True
    backend.press_key.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: True)
    monkeypatch.setattr(server, "_ensure_tabs", _unexpected_shadow_probe)

    result = asyncio.run(server._handle_press_key({"pid": 42, "key": "Enter"}))

    assert result == "pressed Enter"
    backend.activate_window.assert_not_called()
    backend.press_key.assert_called_once_with("return")


def test_coordinate_click_without_pid_fails_before_input(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)

    result = asyncio.run(server._handle_click({"x": 10, "y": 20}))

    assert result == "ERROR: Coordinate click requires pid for focus verification."
    backend.is_available.assert_not_called()
    backend.click.assert_not_called()


def test_explicit_shadow_key_requires_stable_target_and_never_uses_foreground(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)
    probe = AsyncMock(side_effect=AssertionError("provider probed without target"))
    monkeypatch.setattr(server, "_get_cdp_session", probe)

    result = asyncio.run(server._handle_press_key({"key": "Enter", "shadow": True}))

    assert "requires target_id" in result
    probe.assert_not_awaited()
    backend.press_key.assert_not_called()
    backend.hotkey.assert_not_called()


def test_explicit_shadow_key_uses_exact_target_session(monkeypatch):
    from agent_eyes import server

    session = MagicMock()
    session.press_key = AsyncMock()
    tab = MagicMock(id="target-b")
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(
        server._handle_press_key(
            {"key": "Enter", "shadow": True, "target_id": "target-b"}
        )
    )

    assert result == "pressed Enter"
    session.press_key.assert_awaited_once_with("Enter", [])


def test_focus_verification_skips_activation_when_already_frontmost(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    backend.is_frontmost.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "run_native_action_until", _unexpected_shadow_probe)

    assert asyncio.run(server._verify_focus(42)) == (True, "")
    backend.activate_window.assert_not_called()


def test_browser_activation_without_owning_window_identity_fails_before_focus(monkeypatch):
    from agent_eyes import server

    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Unverifiable",
        element=UIElement(id=1, role="tab", platform_ref=object()),
    )
    verify_focus = AsyncMock(side_effect=AssertionError("PID focus must not run"))
    monkeypatch.setattr(server, "_verify_focus", verify_focus)

    assert asyncio.run(server._activate_browser_target_and_wait(target)) is False
    verify_focus.assert_not_awaited()


def test_browser_activation_returns_immediately_when_exact_tab_is_already_active(
    monkeypatch,
):
    from agent_eyes import server

    class InlineWorker:
        async def run(self, call, **_kwargs):
            return call()

    tab = UIElement(id=1, role="tab", platform_ref=object())
    window = UIElement(id=2, role="window", platform_ref=object())
    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Already active",
        element=tab,
        window_element=window,
    )
    adapter = MagicMock()
    adapter.is_window_focused.return_value = True
    adapter.is_element_selected.return_value = True
    verify_focus = AsyncMock(side_effect=AssertionError("PID focus must not run"))
    events = AsyncMock(side_effect=AssertionError("event wrapper must not run"))
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", InlineWorker())
    monkeypatch.setattr(server, "_verify_focus", verify_focus)
    monkeypatch.setattr(server, "run_native_action_until", events)

    assert asyncio.run(server._activate_browser_target_and_wait(target)) is True
    verify_focus.assert_not_awaited()
    events.assert_not_awaited()


def test_foreground_navigate_reuses_native_policy_without_cdp(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_get_cdp_session", _unexpected_shadow_probe)
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    opener = MagicMock(return_value=(True, "Opened in default browser"))
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)

    result = asyncio.run(server._handle_navigate({"url": "https://example.com"}))

    assert "default browser" in result
    opener.assert_called_once_with("https://example.com")


def test_javascript_requires_explicit_shadow_before_connection(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_get_cdp_session", _unexpected_shadow_probe)

    result = asyncio.run(server._handle_evaluate({"expression": "document.title"}))

    assert "shadow=true" in result


def test_web_tree_requires_explicit_shadow_before_connection(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_get_cdp_session", _unexpected_shadow_probe)

    result = asyncio.run(server._handle_get_web_tree({}))

    assert "tree" in result
    assert "shadow=true" in result


def test_close_tab_uses_fresh_native_inventory_and_os_shortcut(monkeypatch):
    from agent_eyes import server

    tab = UIElement(id=1, role="tab", platform_ref=object())
    window = UIElement(id=2, role="window", platform_ref=object())
    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Agent Eyes documentation",
        tab_index=1,
        element=tab,
        window_element=window,
    )
    backend = MagicMock()
    backend.is_available.return_value = True
    backend.hotkey.return_value = True
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.is_window_focused.return_value = True
    adapter.is_element_selected.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [target])
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", AsyncMock(return_value=True))
    monkeypatch.setattr(server, "_ensure_tabs", _unexpected_shadow_probe)

    result = asyncio.run(server._handle_close_tab({"title": "documentation"}))

    assert "Firefox" in result
    server._activate_browser_target_and_wait.assert_awaited_once_with(
        target,
        budget=ANY,
    )
    backend.hotkey.assert_called_once()


def test_close_tab_accepts_stable_native_target_id(monkeypatch):
    from agent_eyes import server

    tab = UIElement(id=1, role="tab", platform_ref=object())
    window = UIElement(id=2, role="window", platform_ref=object())
    target = BrowserTarget(
        browser="Safari",
        pid=71,
        title="Stable target",
        window_index=1,
        tab_index=3,
        element=tab,
        window_element=window,
    )
    backend = MagicMock()
    backend.is_available.return_value = True
    backend.hotkey.return_value = True
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.is_window_focused.return_value = True
    adapter.is_element_selected.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [target])
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", AsyncMock(return_value=True))

    asyncio.run(server._handle_list_tabs({}))

    result = asyncio.run(server._handle_close_tab({"target_id": target.identifier}))

    assert result == "Closed foreground Safari tab."


def test_persistent_shadow_inventory_never_populates_legacy_tab_cache(monkeypatch):
    from agent_eyes import server

    persistent = MagicMock(
        id="persistent-target",
        title="Persistent",
        url="https://example.test",
    )
    pool = MagicMock()
    pool.ensure_connected = AsyncMock()
    pool.is_connected = True
    pool.list_tabs.return_value = [persistent]
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(server, "_cached_tabs", [])
    monkeypatch.setattr(
        server.cdp_client,
        "is_available",
        AsyncMock(side_effect=AssertionError("legacy provider was probed")),
    )

    output = asyncio.run(server._handle_list_tabs({"shadow": True}))

    assert "target_id=persistent-target" in output
    assert server._cached_tabs == []
    server.cdp_client.is_available.assert_not_awaited()


def test_explicit_shadow_inventory_falls_back_to_stable_apple_event_ids(monkeypatch):
    from agent_eyes import applescript, server

    pool = MagicMock()
    pool.ensure_connected = AsyncMock(side_effect=RuntimeError("offline"))
    pool.is_connected = False
    apple = MagicMock()
    apple.is_available.return_value = True
    apple.list_chrome_tabs.return_value = [
        applescript.AppleScriptTab(
            index=0,
            window_index=1,
            title="First",
            url="https://same.test",
            id="tab-a",
            window_id="window-a",
        ),
        applescript.AppleScriptTab(
            index=0,
            window_index=2,
            title="Second",
            url="https://same.test",
            id="tab-b",
            window_id="window-b",
        ),
    ]
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock(return_value=False))
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server.sys, "platform", "darwin")

    output = asyncio.run(server._handle_list_tabs({"shadow": True}))

    assert "target_id=apple-events:window-a:tab-a" in output
    assert "target_id=apple-events:window-b:tab-b" in output


def test_shadow_navigation_requires_target_before_provider_probe(monkeypatch):
    from agent_eyes import server

    provider = AsyncMock(side_effect=AssertionError("provider was probed"))
    monkeypatch.setattr(server, "_get_cdp_session", provider)

    result = asyncio.run(
        server._handle_navigate(
            {"url": "https://example.test", "shadow": True}
        )
    )

    assert "requires target_id" in result
    provider.assert_not_awaited()


def test_apple_navigation_resolves_duplicate_urls_by_exact_stable_id(monkeypatch):
    from agent_eyes import applescript, server

    first = applescript.AppleScriptTab(
        index=0,
        window_index=0,
        title="First",
        url="https://same.test",
        id="tab-a",
        window_id="window-a",
    )
    second = applescript.AppleScriptTab(
        index=0,
        window_index=1,
        title="Second",
        url="https://same.test",
        id="tab-b",
        window_id="window-b",
    )
    apple = MagicMock()
    apple.is_available.return_value = True
    apple.list_chrome_tabs.return_value = [first, second]
    apple.navigate_tab_outcome.return_value = applescript.ShadowExecutionOutcome(
        applescript.ShadowExecutionStatus.CONFIRMED,
        "Navigation dispatched.",
    )
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(None, None, "")),
    )
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_as_tabs_cache", [])

    result = asyncio.run(
        server._handle_navigate(
            {
                "url": "https://after.test",
                "shadow": True,
                "target_id": second.identifier,
            }
        )
    )

    assert result == "Navigation completed in the requested shadow target."
    apple.navigate_tab_outcome.assert_called_once_with(
        "https://after.test",
        tab_index=0,
        window_index=1,
        tab_id="tab-b",
        window_id="window-b",
    )


def test_legacy_apple_shadow_action_requires_exact_stable_target(monkeypatch):
    from agent_eyes import applescript, server

    target = applescript.AppleScriptTab(
        index=2,
        window_index=1,
        title="Target",
        url="https://example.test",
        id="tab-c",
        window_id="window-c",
    )
    apple = MagicMock()
    apple.is_available.return_value = True
    apple.list_chrome_tabs.return_value = [target]
    apple.shadow_click_outcome.return_value = applescript.ShadowExecutionOutcome(
        applescript.ShadowExecutionStatus.CONFIRMED,
        "clicked",
    )
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_as_tabs_cache", [])

    missing = asyncio.run(
        server._handle_shadow_async({"action": "click", "selector": "button"})
    )
    result = asyncio.run(
        server._handle_shadow_async(
            {
                "action": "click",
                "selector": "button",
                "target_id": target.identifier,
            }
        )
    )

    assert "requires target_id" in missing
    assert result == "Shadow click completed."
    apple.shadow_click_outcome.assert_called_once_with(
        "button",
        tab_index=2,
        window_index=1,
        tab_id="tab-c",
        window_id="window-c",
    )


def test_legacy_apple_shadow_empty_read_does_not_report_provider_unavailable(monkeypatch):
    from agent_eyes import applescript, server

    target = applescript.AppleScriptTab(
        index=0,
        window_index=0,
        title="Empty",
        url="https://example.test",
        id="tab-empty",
        window_id="window-empty",
    )
    apple = MagicMock()
    apple.is_available.return_value = True
    apple.list_chrome_tabs.return_value = [target]
    apple.shadow_read_interactive.return_value = ""
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_as_tabs_cache", [])

    result = asyncio.run(
        server._handle_shadow_async(
            {"action": "read", "target_id": target.identifier}
        )
    )

    assert result == "No interactive elements found."


def test_scroll_defaults_to_foreground_input_without_pid(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    backend.scroll.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "_ensure_tabs", _unexpected_shadow_probe)

    result = asyncio.run(server._handle_scroll({"delta_y": 300}))

    assert result == "scrolled down"
    backend.scroll.assert_called_once_with(400, 400, delta_x=0, delta_y=-3)


def test_find_rejects_regular_expressions_to_keep_event_loop_bounded(monkeypatch):
    from agent_eyes import server

    result = server._handle_find({"name": "(a+)+$", "match": "regex"})
    assert result.startswith("ERROR: match must be one of")


def test_drag_defaults_to_foreground_input_before_shadow(monkeypatch):
    from agent_eyes import server

    backend = MagicMock()
    backend.is_available.return_value = True
    backend.drag.return_value = True
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "_ensure_tabs", _unexpected_shadow_probe)

    result = asyncio.run(
        server._handle_drag({"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4})
    )

    assert "Dragged" in result
    backend.drag.assert_called_once_with(1, 2, 3, 4)


def test_protocol_only_handlers_refuse_implicit_shadow(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_ensure_tabs", _unexpected_shadow_probe)
    monkeypatch.setattr(server, "_get_cdp_session", _unexpected_shadow_probe)

    dialog = asyncio.run(server._handle_dialog({}))
    pierce = asyncio.run(server._handle_pierce({"selector": "x-widget"}))

    assert "shadow=true" in dialog
    assert "shadow=true" in pierce


def test_shadow_type_verifies_protocol_result_without_fixed_sleep(monkeypatch):
    from agent_eyes import server

    from agent_eyes.coordinator import AutomationCoordinator
    from agent_eyes.observations import ElementRecord
    from agent_eyes.operation import OperationMode

    element = UIElement(
        id=801,
        role="textbox",
        name="Search",
        source="cdp",
        platform_ref=99,
        tab_index=0,
    )
    coordinator = AutomationCoordinator()
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=1,
        revision=1,
        elements=[ElementRecord(local_id=element.id, value=element)],
    )
    monkeypatch.setattr(server, "coordinator", coordinator)
    session = MagicMock()
    session.target_id = "target-1"
    session.generation = 1
    session.enable_domain = AsyncMock()
    session.send = AsyncMock(
        side_effect=[
            {"object": {"objectId": "object-1"}},
            {
                "nodes": [
                    {
                        "backendDOMNodeId": 99,
                        "ignored": False,
                        "role": {"value": "textbox"},
                        "name": {"value": "Search"},
                        "properties": [],
                    }
                ]
            },
            {},
            {
                "nodes": [
                    {
                        "backendDOMNodeId": 99,
                        "ignored": False,
                        "role": {"value": "textbox"},
                        "name": {"value": "Search"},
                        "properties": [
                            {
                                "name": "focused",
                                "value": {
                                    "type": "booleanOrUndefined",
                                    "value": True,
                                },
                            }
                        ],
                    }
                ]
            },
            {},
            {"result": {"value": "hello"}},
        ]
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=1),
    )

    async def unexpected_sleep(*args, **kwargs):
        raise AssertionError("type orchestration used a fixed sleep")

    monkeypatch.setattr(server.asyncio, "sleep", unexpected_sleep)

    result = asyncio.run(
        server._handle_type(
            {
                "id": 801,
                "text": "hello",
                "snapshot": snapshot.token,
                "shadow": True,
            }
        )
    )

    assert result == "typed 5 characters into [801]"


def test_shadow_javascript_uncertain_outcome_never_replays_in_applescript(monkeypatch):
    from agent_eyes import server

    session = MagicMock()
    session.enable_domain = AsyncMock()
    session.send = AsyncMock(side_effect=asyncio.TimeoutError)
    tab = MagicMock(id="target-1")
    apple = MagicMock()
    apple.is_available.return_value = True
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(
        server._handle_evaluate(
            {
                "expression": "globalThis.sideEffect = 1",
                "shadow": True,
                "target_id": "target-1",
            }
        )
    )

    assert "OUTCOME_UNKNOWN" in result
    apple.shadow_execute_js_outcome.assert_not_called()


def test_shadow_navigation_uncertain_outcome_never_replays(monkeypatch):
    from agent_eyes import server

    session = MagicMock()
    session.enable_domain = AsyncMock()
    session.send = AsyncMock(return_value={})

    async def dispatched_then_timeout(_methods, action, *, timeout):
        await action()
        raise asyncio.TimeoutError

    session.run_until_event = AsyncMock(side_effect=dispatched_then_timeout)
    tab = MagicMock(id="target-1")
    apple = MagicMock()
    apple.is_available.return_value = True
    legacy = AsyncMock(side_effect=AssertionError("legacy replayed navigation"))
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(server.cdp_client, "navigate", legacy)
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(
        server._handle_navigate(
            {
                "url": "https://example.test",
                "shadow": True,
                "target_id": "target-1",
            }
        )
    )

    assert "OUTCOME_UNKNOWN" in result
    legacy.assert_not_awaited()
    apple.navigate_tab_outcome.assert_not_called()
