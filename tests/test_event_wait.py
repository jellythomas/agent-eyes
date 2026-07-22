from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

from mcp.types import CallToolResult

from agent_eyes.adapters.base import UIElement
from agent_eyes.cdp import CDPClient, ChromeTab
from agent_eyes.cdp_persistent import CDPSession
from agent_eyes.native_events import NativeWaitResult
from agent_eyes.operation import OperationError, OperationErrorCode


def make_session() -> CDPSession:
    connection = MagicMock()
    connection._next_id.return_value = 1
    connection._send_raw = AsyncMock()
    return CDPSession("session-1", connection)


def test_event_listener_removal_is_idempotent_and_preserves_others():
    session = make_session()
    first = MagicMock()
    second = MagicMock()
    session.on_event("Accessibility.nodesUpdated", first)
    session.on_event("Accessibility.nodesUpdated", second)

    session.off_event("Accessibility.nodesUpdated", first)
    session.off_event("Accessibility.nodesUpdated", first)
    session._on_message({"method": "Accessibility.nodesUpdated", "params": {}})

    first.assert_not_called()
    second.assert_called_once_with({})


def test_run_until_event_subscribes_before_action_and_cleans_up():
    async def run():
        session = make_session()

        async def action():
            session._on_message(
                {"method": "Page.loadEventFired", "params": {"timestamp": 12}}
            )
            return {"frameId": "frame"}

        action_result, event = await session.run_until_event(
            "Page.loadEventFired",
            action,
            timeout=1,
        )

        assert action_result == {"frameId": "frame"}
        assert event == {"timestamp": 12}
        assert "Page.loadEventFired" not in session._event_handlers

    asyncio.run(run())


def test_ax_wait_returns_immediate_match_with_root_scoped_query():
    async def run():
        session = make_session()
        session.enable_domain = AsyncMock()
        matched = {
            "nodeId": "ax-2",
            "backendDOMNodeId": 12,
            "role": {"value": "button"},
            "name": {"value": "Continue"},
        }
        session.send = AsyncMock(
            side_effect=[
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": [matched]},
            ]
        )

        result = await session.wait_for_ax_node(
            role="button", name="Continue", timeout=1
        )

        assert result == matched
        session.send.assert_any_await(
            "Accessibility.queryAXTree",
            {"backendNodeId": 7, "role": "button", "accessibleName": "Continue"},
        )
        assert session._event_handlers == {}

    asyncio.run(run())


def test_ax_wait_requeries_only_after_accessibility_event():
    async def run():
        session = make_session()
        session.enable_domain = AsyncMock()
        matched = {
            "nodeId": "ax-3",
            "backendDOMNodeId": 13,
            "role": {"value": "heading"},
            "name": {"value": "Loaded"},
        }
        session.send = AsyncMock(
            side_effect=[
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": []},
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": [matched]},
            ]
        )

        task = asyncio.create_task(
            session.wait_for_ax_node(role="heading", name="Loaded", timeout=1)
        )
        while session.send.await_count < 2:
            await asyncio.sleep(0)
        assert session.send.await_count == 2

        session._on_message({"method": "Accessibility.nodesUpdated", "params": {}})
        result = await task

        assert result == matched
        assert session.send.await_count == 4
        assert session._event_handlers == {}

    asyncio.run(run())


def test_ax_wait_timeout_removes_all_listeners():
    async def run():
        session = make_session()
        session.enable_domain = AsyncMock()
        session.send = AsyncMock(
            side_effect=[
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": []},
            ]
        )

        result = await session.wait_for_ax_node(role="alert", timeout=0.01)

        assert result is None
        assert session._event_handlers == {}
        assert session.send.await_count == 2

    asyncio.run(run())


def test_legacy_ax_wait_rechecks_on_protocol_event_without_poll_sleep(monkeypatch):
    async def run():
        import websockets

        class FakeWebSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def recv(self):
                return '{"method":"Accessibility.nodesUpdated","params":{}}'

        client = CDPClient()
        fake_ws = FakeWebSocket()
        matched = {
            "nodeId": "ax-4",
            "backendDOMNodeId": 14,
            "role": {"value": "button"},
            "name": {"value": "Save"},
        }
        client._send = AsyncMock(
            side_effect=[
                {},
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": []},
                {"node": {"backendDOMNodeId": 7}},
                {"nodes": [matched]},
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda url, **_kwargs: fake_ws,
        )

        async def unexpected_sleep(*args, **kwargs):
            raise AssertionError("legacy shadow wait used polling sleep")

        monkeypatch.setattr("agent_eyes.cdp.asyncio.sleep", unexpected_sleep)
        tab = ChromeTab(
            "tab",
            "Example",
            "https://example.com",
            "ws://127.0.0.1:9222/devtools/page/tab",
        )

        result = await client.wait_for_element(
            tab, role="button", name="Save", timeout=1
        )

        assert result is not None
        assert result.role == "button"
        assert result.name == "Save"
        client._send.assert_any_await(
            fake_ws,
            "Accessibility.queryAXTree",
            {
                "backendNodeId": 7,
                "role": "button",
                "accessibleName": "Save",
            },
            event_buffer=ANY,
        )

    asyncio.run(run())


def test_cancelling_send_cleans_pending_future():
    async def run():
        session = make_session()

        task = asyncio.create_task(
            session.send("Runtime.evaluate", {"expression": "1"})
        )
        await asyncio.sleep(0)
        assert session._pending

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert session._pending == {}

    asyncio.run(run())


def test_default_wait_does_not_probe_or_connect_shadow_provider(monkeypatch):
    async def run():
        from agent_eyes import server

        monkeypatch.setattr(server, "_ensure_tabs", AsyncMock())
        monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())

        result = await server._handle_wait_for({"role": "button", "timeout": 0.01})

        assert result.startswith("ERROR:")
        assert "pid" in result.lower()
        server._ensure_tabs.assert_not_awaited()
        server.cdp_pool.ensure_connected.assert_not_awaited()

    asyncio.run(run())


def test_foreground_wait_uses_native_event_broker_and_registers_match(monkeypatch):
    async def run():
        from agent_eyes import server

        element = UIElement(id=31, role="button", name="Continue")
        waiter = AsyncMock(
            return_value=NativeWaitResult(
                element=element,
                elapsed=0.012,
                event_driven=True,
                checks=2,
            )
        )
        register = MagicMock()
        monkeypatch.setattr(server, "native_adapter", MagicMock())
        monkeypatch.setattr(server, "wait_for_native_element", waiter)
        monkeypatch.setattr(server.registry, "register_element", register)
        monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())
        monkeypatch.setattr(server, "coordinator", server.AutomationCoordinator())

        result = await server._handle_wait_for(
            {"pid": 55, "role": "button", "name": "Continue", "timeout": 3}
        )

        assert "native events" in result
        waiter.assert_awaited_once_with(
            server.native_adapter,
            55,
            role="button",
            name="Continue",
            timeout=3.0,
            budget=ANY,
            worker=server.native_worker,
        )
        assert result.startswith("snapshot=")
        register.assert_called_once_with(element)
        server.cdp_pool.ensure_connected.assert_not_awaited()

    asyncio.run(run())


def test_foreground_native_wait_timeout_never_probes_shadow(monkeypatch):
    async def run():
        from agent_eyes import server

        waiter = AsyncMock(
            return_value=NativeWaitResult(
                element=None,
                elapsed=0.02,
                event_driven=True,
                checks=1,
            )
        )
        monkeypatch.setattr(server, "native_adapter", MagicMock())
        monkeypatch.setattr(server, "wait_for_native_element", waiter)
        monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())

        result = await server._handle_wait_for(
            {"pid": 56, "name": "Missing", "timeout": 0.02}
        )

        assert result.startswith("ERROR: Timeout after 0.02s")
        server.cdp_pool.ensure_connected.assert_not_awaited()

    asyncio.run(run())


def test_foreground_wait_missing_pid_is_an_mcp_tool_error(monkeypatch):
    async def run():
        from agent_eyes import server

        monkeypatch.setattr(
            server,
            "_ensure_runtime_readiness",
            AsyncMock(return_value=SimpleNamespace(core_ready=True)),
        )

        result = await server.call_tool("wait", {"role": "button"})

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.content[0].text.startswith("ERROR:")
        assert "pid" in result.content[0].text.lower()

    asyncio.run(run())


def test_foreground_wait_timeout_is_an_mcp_tool_error(monkeypatch):
    async def run():
        from agent_eyes import server

        waiter = AsyncMock(
            return_value=NativeWaitResult(
                element=None,
                elapsed=0.01,
                event_driven=True,
                checks=1,
            )
        )
        monkeypatch.setattr(server, "native_adapter", MagicMock())
        monkeypatch.setattr(server, "wait_for_native_element", waiter)
        monkeypatch.setattr(
            server,
            "_ensure_runtime_readiness",
            AsyncMock(return_value=SimpleNamespace(core_ready=True)),
        )

        result = await server.call_tool(
            "wait",
            {"pid": 56, "name": "Missing", "timeout": 0.01},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.content[0].text.startswith("ERROR: Timeout after 0.01s")

    asyncio.run(run())


def test_foreground_wait_reports_missing_native_provider_for_supplied_pid(monkeypatch):
    async def run():
        from agent_eyes import server

        monkeypatch.setattr(server, "native_adapter", None)
        monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())

        result = await server._handle_wait_for({"pid": 56, "name": "Missing"})

        assert "native" in result.lower()
        assert "unavailable" in result.lower()
        server.cdp_pool.ensure_connected.assert_not_awaited()

    asyncio.run(run())


def test_explicit_shadow_wait_uses_connected_event_session(monkeypatch):
    async def run():
        from agent_eyes import server

        raw_node = {
            "backendDOMNodeId": 12,
            "role": {"value": "button"},
            "name": {"value": "Continue"},
        }
        session = MagicMock()
        session.generation = 4
        session.wait_for_ax_node = AsyncMock(return_value=raw_node)
        monkeypatch.setattr(
            server,
            "_get_cdp_session",
            AsyncMock(return_value=(session, MagicMock(id="target-7"), "")),
        )
        monkeypatch.setattr(
            server,
            "_persistent_document_revision",
            AsyncMock(return_value=12),
        )
        monkeypatch.setattr(server, "coordinator", server.AutomationCoordinator())

        result = await server._handle_wait_for(
            {
                "role": "button",
                "name": "Continue",
                "shadow": True,
                "target_id": "target-7",
            }
        )

        assert result.startswith("snapshot=")
        assert "Continue" in result
        session.wait_for_ax_node.assert_awaited_once()

    asyncio.run(run())


def test_shadow_wait_timeout_is_an_mcp_tool_error(monkeypatch):
    async def run():
        from agent_eyes import server

        session = MagicMock()
        session.generation = 4
        session.wait_for_ax_node = AsyncMock(return_value=None)
        monkeypatch.setattr(
            server,
            "_get_cdp_session",
            AsyncMock(return_value=(session, MagicMock(id="target-7"), "")),
        )
        monkeypatch.setattr(
            server,
            "_persistent_document_revision",
            AsyncMock(return_value=12),
        )
        monkeypatch.setattr(server, "coordinator", server.AutomationCoordinator())

        result = await server.call_tool(
            "wait",
            {
                "role": "button",
                "shadow": True,
                "target_id": "target-7",
                "timeout": 0.01,
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.content[0].text.startswith("ERROR: Timeout")

    asyncio.run(run())


def test_short_shadow_wait_does_not_poison_longer_timeout(monkeypatch):
    async def run():
        from agent_eyes import server

        raw_node = {
            "backendDOMNodeId": 12,
            "role": {"value": "button"},
            "name": {"value": "Continue"},
        }
        first_started = asyncio.Event()
        timeouts: list[float] = []

        async def wait_for_ax_node(*, role, name, timeout):
            assert role == "button"
            assert name == "Continue"
            timeouts.append(timeout)
            first_started.set()
            await asyncio.sleep(0.05)
            return raw_node

        session = MagicMock()
        session.generation = 4
        session.wait_for_ax_node = AsyncMock(side_effect=wait_for_ax_node)
        monkeypatch.setattr(
            server,
            "_get_cdp_session",
            AsyncMock(return_value=(session, MagicMock(id="target-7"), "")),
        )
        monkeypatch.setattr(
            server,
            "_persistent_document_revision",
            AsyncMock(return_value=12),
        )
        monkeypatch.setattr(server, "coordinator", server.AutomationCoordinator())

        short = asyncio.create_task(
            server._handle_wait_for(
                {
                    "role": "button",
                    "name": "Continue",
                    "shadow": True,
                    "target_id": "target-7",
                    "timeout": 0.01,
                }
            )
        )
        await first_started.wait()
        long = asyncio.create_task(
            server._handle_wait_for(
                {
                    "role": "button",
                    "name": "Continue",
                    "shadow": True,
                    "target_id": "target-7",
                    "timeout": 1.0,
                }
            )
        )

        with pytest.raises(OperationError) as exc_info:
            await short
        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        output = await long

        assert output.startswith("snapshot=")
        assert sorted(timeouts) == [0.01, 1.0]

    import pytest

    asyncio.run(run())
