"""Tests for CDPConnection and CDPSession — persistent WebSocket CDP client.

All tests mock websockets.connect so they run without a real Chrome instance.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_eyes.cdp import (
    _MAX_PIERCED_DOM_NODES,
    _PIERCED_CONTROL_SELECTOR,
    _PIERCED_SELECTOR_DEPTH,
)
from agent_eyes.cdp_persistent import (
    _DOM_DESCRIBE_CONCURRENCY,
    CDPConnection,
    CDPSession,
    ChromeTab,
)


def test_cancelled_sent_command_remains_pending_until_the_transport_settles() -> None:
    async def run() -> None:
        sent = asyncio.Event()

        class Connection:
            def __init__(self) -> None:
                self.message_id = 0

            def _next_id(self) -> int:
                self.message_id += 1
                return self.message_id

            async def _send_raw(self, _session_id, _msg_id, _method, _params) -> None:
                sent.set()

        session = CDPSession("session-1", Connection())
        command = asyncio.create_task(session.send("Runtime.callFunctionOn"))
        await sent.wait()
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

        assert session.has_pending_commands is True
        recovery = asyncio.create_task(session.wait_until_idle())
        await asyncio.sleep(0)
        assert recovery.done() is False

        session._on_message({"id": 1, "result": {}})
        await recovery
        assert session.has_pending_commands is False

    asyncio.run(run())


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_response(msg_id: int, result: dict, session_id: str = "") -> str:
    """Build a JSON-encoded CDP response string."""
    msg: dict[str, Any] = {"id": msg_id, "result": result}
    if session_id:
        msg["sessionId"] = session_id
    return json.dumps(msg)


def make_event(method: str, params: dict, session_id: str = "") -> str:
    """Build a JSON-encoded CDP event string."""
    msg: dict[str, Any] = {"method": method, "params": params}
    if session_id:
        msg["sessionId"] = session_id
    return json.dumps(msg)


def make_attached_event(
    target_id: str, session_id: str, url: str = "https://example.com", title: str = "Example"
) -> str:
    return make_event(
        "Target.attachedToTarget",
        {
            "sessionId": session_id,
            "targetInfo": {
                "targetId": target_id,
                "type": "page",
                "url": url,
                "title": title,
                "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{target_id}",
            },
        },
    )


def make_detached_event(target_id: str, session_id: str) -> str:
    return make_event(
        "Target.detachedFromTarget",
        {"sessionId": session_id, "targetId": target_id},
    )


class FakeWebSocket:
    """Minimal WebSocket stub that lets tests inject messages."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self._messages = list(messages or [])
        self.sent: list[str] = []
        self._closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        for m in self._messages:
            self._queue.put_nowait(m)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def push(self, message: str) -> None:
        """Inject a message that the read loop will receive."""
        self._queue.put_nowait(message)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # Block until a message arrives or the queue is drained
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            raise StopAsyncIteration


# ── ChromeTab ─────────────────────────────────────────────────────────────────


class TestChromeTab:
    def test_fields(self):
        tab = ChromeTab(
            id="abc",
            title="My Tab",
            url="https://example.com",
            ws_url="ws://127.0.0.1:9222/devtools/page/abc",
            session_id="sess1",
        )
        assert tab.id == "abc"
        assert tab.title == "My Tab"
        assert tab.url == "https://example.com"
        assert tab.ws_url == "ws://127.0.0.1:9222/devtools/page/abc"
        assert tab.session_id == "sess1"

    def test_default_session_id_empty(self):
        tab = ChromeTab(id="x", title="", url="", ws_url="")
        assert tab.session_id == ""


# ── CDPSession ────────────────────────────────────────────────────────────────


class TestCDPSessionMessageRouting:
    def _make_session(self) -> tuple[CDPSession, MagicMock]:
        conn = MagicMock()
        conn._next_id = AsyncMock(return_value=1)
        conn._send_raw = AsyncMock()
        session = CDPSession("sess-abc", conn)
        return session, conn

    def test_response_resolves_pending_future(self):
        session, _ = self._make_session()
        loop = asyncio.new_event_loop()

        async def run():
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            session._pending[42] = future
            session._on_message({"id": 42, "result": {"value": 7}})
            return await future

        result = loop.run_until_complete(run())
        loop.close()
        assert result == {"id": 42, "result": {"value": 7}}

    def test_unknown_response_id_is_ignored(self):
        session, _ = self._make_session()
        # Should not raise
        session._on_message({"id": 999, "result": {}})

    def test_event_dispatched_to_handler(self):
        session, _ = self._make_session()
        received = []
        session.on_event("Page.loadEventFired", lambda p: received.append(p))
        session._on_message({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
        assert received == [{"timestamp": 1.0}]

    def test_event_no_handler_is_ignored(self):
        session, _ = self._make_session()
        # No handler registered — should not raise
        session._on_message({"method": "Page.someUnknownEvent", "params": {}})

    def test_multiple_handlers_for_same_event(self):
        session, _ = self._make_session()
        results = []
        session.on_event("Network.responseReceived", lambda p: results.append("A"))
        session.on_event("Network.responseReceived", lambda p: results.append("B"))
        session._on_message({"method": "Network.responseReceived", "params": {}})
        assert results == ["A", "B"]

    def test_done_future_is_not_set_again(self):
        session, _ = self._make_session()
        loop = asyncio.new_event_loop()

        async def run():
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            future.set_result("already done")
            session._pending[1] = future
            # Should not raise InvalidStateError
            session._on_message({"id": 1, "result": {}})

        loop.run_until_complete(run())
        loop.close()


class TestCDPSessionDomainCaching:
    def _make_session(self, send_result: dict | None = None) -> CDPSession:
        conn = MagicMock()
        conn._next_id = AsyncMock(side_effect=lambda: asyncio.get_event_loop().run_until_complete(
            asyncio.coroutine(lambda: 1)()
        ))
        send_mock = AsyncMock(return_value=send_result or {})
        session = CDPSession("sess-xyz", conn)
        session.send = send_mock
        return session

    def test_enable_domain_calls_send_once(self):
        loop = asyncio.new_event_loop()

        async def run():
            session = CDPSession("s1", MagicMock())
            session.send = AsyncMock(return_value={})
            await session.enable_domain("DOM")
            await session.enable_domain("DOM")  # second call — must NOT resend
            assert session.send.call_count == 1
            session.send.assert_called_once_with("DOM.enable", idempotent=True)

        loop.run_until_complete(run())
        loop.close()


class TestCDPSessionDomSearch:
    def test_query_is_protocol_data_and_results_keep_backend_identity(self):
        async def run():
            session = CDPSession("s-search", MagicMock())
            query = 'button[data-name="] ; globalThis.pwned = true; //"]'

            async def send(method, params=None, *, idempotent=False):
                if method == "DOM.performSearch":
                    assert params == {
                        "query": query,
                        "includeUserAgentShadowDOM": True,
                    }
                    assert idempotent is True
                    return {"searchId": "search-1", "resultCount": 2}
                if method == "DOM.getSearchResults":
                    assert params == {
                        "searchId": "search-1",
                        "fromIndex": 0,
                        "toIndex": 2,
                    }
                    assert idempotent is True
                    return {"nodeIds": [11, 12]}
                if method == "DOM.describeNode":
                    assert idempotent is True
                    node_id = params["nodeId"]
                    return {
                        "node": {
                            "nodeId": node_id,
                            "backendNodeId": node_id + 100,
                            "nodeType": 1,
                            "nodeName": "BUTTON",
                            "attributes": ["aria-label", f"Button {node_id}"],
                        }
                    }
                if method == "DOM.discardSearchResults":
                    assert params == {"searchId": "search-1"}
                    assert idempotent is False
                    return {}
                raise AssertionError(method)

            session.enable_domain = AsyncMock()
            session.send = AsyncMock(side_effect=send)

            nodes = await session.search_dom(query)

            session.enable_domain.assert_awaited_once_with("DOM")
            assert [node["backendNodeId"] for node in nodes] == [111, 112]

        asyncio.run(run())

    def test_concurrent_searches_bound_shared_socket_and_preserve_results(self):
        async def run():
            connection = CDPConnection()
            connection._generation = 1
            connection._on_attached(
                {
                    "sessionId": "session-a",
                    "targetInfo": {
                        "targetId": "target-a",
                        "type": "page",
                    },
                }
            )
            connection._on_attached(
                {
                    "sessionId": "session-b",
                    "targetInfo": {
                        "targetId": "target-b",
                        "type": "page",
                    },
                }
            )
            first = connection.get_session_for_target("target-a")
            second = connection.get_session_for_target("target-b")
            assert first is not None
            assert second is not None

            in_flight = 0
            peak_in_flight = 0

            def protocol(start: int):
                async def send(method, params=None, *, idempotent=False):
                    nonlocal in_flight, peak_in_flight
                    if method == "DOM.performSearch":
                        return {"searchId": f"search-{start}", "resultCount": 500}
                    if method == "DOM.getSearchResults":
                        return {"nodeIds": list(range(start, start + 500))}
                    if method == "DOM.describeNode":
                        assert idempotent is True
                        in_flight += 1
                        peak_in_flight = max(peak_in_flight, in_flight)
                        await asyncio.sleep(0)
                        in_flight -= 1
                        return {"node": {"nodeId": params["nodeId"]}}
                    if method == "DOM.discardSearchResults":
                        return {}
                    raise AssertionError(method)

                return send

            first.enable_domain = AsyncMock()
            second.enable_domain = AsyncMock()
            first.send = AsyncMock(side_effect=protocol(1))
            second.send = AsyncMock(side_effect=protocol(501))

            first_nodes, second_nodes = await asyncio.gather(
                first.search_dom("button", max_results=500),
                second.search_dom("button", max_results=500),
            )

            assert peak_in_flight == _DOM_DESCRIBE_CONCURRENCY
            assert [node["nodeId"] for node in first_nodes] == list(range(1, 501))
            assert [node["nodeId"] for node in second_nodes] == list(
                range(501, 1_001)
            )

        asyncio.run(run())


class TestCDPSessionPiercedDocument:
    def test_named_control_search_is_capped_before_node_conversion(self):
        async def run():
            session = CDPSession("s-pierce", MagicMock())
            session.search_dom = AsyncMock(
                return_value=[
                    {"nodeId": 2, "backendNodeId": 20},
                    {"nodeId": 3, "backendNodeId": 30},
                ]
            )

            nodes = await session.get_pierced_dom()

            session.search_dom.assert_awaited_once_with(
                _PIERCED_CONTROL_SELECTOR,
                max_results=_MAX_PIERCED_DOM_NODES,
            )
            assert [node["backendNodeId"] for node in nodes] == [20, 30]

        asyncio.run(run())

    def test_mutation_helpers_dispatch_once_without_idempotent_replay(self):
        async def run():
            session = CDPSession("s-mutate", MagicMock())
            session.send = AsyncMock(return_value={})

            await session.press_key("Enter", ["Shift"])
            await session.scroll(10, 20, 0, 300)
            await session.handle_dialog(True, "answer")
            await session.set_file_input(77, ["/tmp/example.txt"])

            methods = [call.args[0] for call in session.send.await_args_list]
            assert methods == [
                "Input.dispatchKeyEvent",
                "Input.dispatchKeyEvent",
                "Input.dispatchMouseEvent",
                "Page.handleJavaScriptDialog",
                "DOM.setFileInputFiles",
            ]
            assert all(
                call.kwargs.get("idempotent", False) is False
                for call in session.send.await_args_list
            )

        asyncio.run(run())

    def test_pierce_selector_scopes_describe_to_exact_search_matches(self):
        async def run():
            session = CDPSession("s-selector", MagicMock())
            session.search_dom = AsyncMock(
                return_value=[{"nodeId": 10, "backendNodeId": 20}]
            )
            session.send = AsyncMock(
                return_value={
                    "node": {
                        "nodeId": 10,
                        "backendNodeId": 20,
                        "shadowRoots": [
                            {
                                "nodeId": 11,
                                "backendNodeId": 21,
                                "nodeType": 1,
                                "nodeName": "BUTTON",
                                "attributes": ["aria-label", "Inside"],
                            }
                        ],
                    }
                }
            )

            nodes = await session.pierce_selector("custom-shell")

            session.search_dom.assert_awaited_once_with(
                "custom-shell",
                max_results=20,
            )
            session.send.assert_awaited_once_with(
                "DOM.describeNode",
                {
                    "backendNodeId": 20,
                    "depth": _PIERCED_SELECTOR_DEPTH,
                    "pierce": True,
                },
                idempotent=True,
            )
            assert [node["backendNodeId"] for node in nodes] == [20, 21]

        asyncio.run(run())

    def test_result_count_is_capped_and_search_is_always_discarded(self):
        async def run():
            session = CDPSession("s-search", MagicMock())

            async def send(method, params=None, *, idempotent=False):
                if method == "DOM.performSearch":
                    return {"searchId": "search-2", "resultCount": 10_000}
                if method == "DOM.getSearchResults":
                    assert params["toIndex"] == 3
                    return {"nodeIds": [1, 2, 3]}
                if method == "DOM.describeNode":
                    raise RuntimeError("describe failed")
                if method == "DOM.discardSearchResults":
                    return {}
                raise AssertionError(method)

            session.enable_domain = AsyncMock()
            session.send = AsyncMock(side_effect=send)

            with pytest.raises(RuntimeError, match="describe failed"):
                await session.search_dom("button", max_results=3)

            assert any(
                call.args[0] == "DOM.discardSearchResults"
                for call in session.send.await_args_list
            )

        asyncio.run(run())

    @pytest.mark.parametrize("query", ["", "   ", None])
    def test_empty_queries_fail_before_protocol_dispatch(self, query):
        async def run():
            session = CDPSession("s-search", MagicMock())
            session.send = AsyncMock()

            with pytest.raises(ValueError, match="non-empty"):
                await session.search_dom(query)

            session.send.assert_not_awaited()

        asyncio.run(run())


class TestCDPSessionDomainCachingAdditional:
    def test_different_domains_each_enabled_once(self):
        loop = asyncio.new_event_loop()

        async def run():
            session = CDPSession("s2", MagicMock())
            session.send = AsyncMock(return_value={})
            await session.enable_domain("DOM")
            await session.enable_domain("CSS")
            await session.enable_domain("DOM")
            await session.enable_domain("CSS")
            assert session.send.call_count == 2

        loop.run_until_complete(run())
        loop.close()

    def test_enabled_domains_tracked(self):
        loop = asyncio.new_event_loop()

        async def run():
            session = CDPSession("s3", MagicMock())
            session.send = AsyncMock(return_value={})
            await session.enable_domain("Accessibility")
            assert "Accessibility" in session._enabled_domains

        loop.run_until_complete(run())
        loop.close()


# ── CDPConnection state management ───────────────────────────────────────────


class TestCDPConnectionDefaults:
    def test_not_connected_by_default(self):
        conn = CDPConnection()
        assert conn.is_connected is False

    def test_default_port(self):
        conn = CDPConnection()
        assert conn.active_port == 9222

    def test_custom_port(self):
        conn = CDPConnection(port=9333)
        assert conn.active_port == 9333

    def test_no_tabs_initially(self):
        conn = CDPConnection()
        assert conn.list_tabs() == []

    def test_no_sessions_initially(self):
        conn = CDPConnection()
        assert conn._sessions == {}


class TestCDPConnectionOnAttached:
    def test_attaches_page_target(self):
        conn = CDPConnection()
        conn._on_attached({
            "sessionId": "sess-1",
            "targetInfo": {
                "targetId": "target-1",
                "type": "page",
                "url": "https://example.com",
                "title": "Example",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target-1",
            },
        })
        assert len(conn._tabs) == 1
        assert len(conn._sessions) == 1
        tab = conn._tabs[0]
        assert tab.id == "target-1"
        assert tab.session_id == "sess-1"
        assert tab.url == "https://example.com"
        assert "sess-1" in conn._sessions

    def test_ignores_non_page_target(self):
        conn = CDPConnection()
        conn._on_attached({
            "sessionId": "sess-sw",
            "targetInfo": {
                "targetId": "sw-1",
                "type": "service_worker",
                "url": "https://example.com/sw.js",
                "title": "",
                "webSocketDebuggerUrl": "",
            },
        })
        assert conn._tabs == []
        assert conn._sessions == {}

    def test_ignores_missing_session_id(self):
        conn = CDPConnection()
        conn._on_attached({
            "sessionId": "",
            "targetInfo": {
                "targetId": "t1",
                "type": "page",
                "url": "https://example.com",
                "title": "X",
                "webSocketDebuggerUrl": "",
            },
        })
        assert conn._tabs == []

    def test_multiple_tabs_tracked_in_order(self):
        conn = CDPConnection()
        for i in range(3):
            conn._on_attached({
                "sessionId": f"sess-{i}",
                "targetInfo": {
                    "targetId": f"target-{i}",
                    "type": "page",
                    "url": f"https://tab{i}.com",
                    "title": f"Tab {i}",
                    "webSocketDebuggerUrl": "",
                },
            })
        assert len(conn._tabs) == 3
        assert conn._tabs[0].url == "https://tab0.com"
        assert conn._tabs[2].url == "https://tab2.com"


class TestCDPConnectionOnDetached:
    def _attach(self, conn: CDPConnection, target_id: str, session_id: str) -> None:
        conn._on_attached({
            "sessionId": session_id,
            "targetInfo": {
                "targetId": target_id,
                "type": "page",
                "url": "https://example.com",
                "title": "Tab",
                "webSocketDebuggerUrl": "",
            },
        })

    def test_detach_removes_session_and_tab(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "s1")
        conn._on_detached({"sessionId": "s1", "targetId": "t1"})
        assert conn._tabs == []
        assert "s1" not in conn._sessions
        assert "t1" not in conn._tab_by_target

    def test_detach_unknown_target_is_safe(self):
        conn = CDPConnection()
        # Should not raise
        conn._on_detached({"sessionId": "ghost-sess", "targetId": "ghost-target"})

    def test_detach_only_removes_correct_tab(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "s1")
        self._attach(conn, "t2", "s2")
        conn._on_detached({"sessionId": "s1", "targetId": "t1"})
        assert len(conn._tabs) == 1
        assert conn._tabs[0].id == "t2"
        assert "s2" in conn._sessions

    def test_late_detach_for_replaced_session_keeps_current_target_binding(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "session-old")
        self._attach(conn, "t1", "session-new")

        conn._on_detached({"sessionId": "session-old", "targetId": "t1"})

        current = conn.get_session_for_target("t1")
        assert current is not None
        assert current.session_id == "session-new"
        assert len(conn.list_tabs()) == 1

    def test_detach_can_resolve_target_from_session_identity(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "s1")

        conn._on_detached({"sessionId": "s1"})

        assert conn.get_session_for_target("t1") is None
        assert conn.list_tabs() == []


class TestCDPConnectionGetSessionForTab:
    def _attach(self, conn: CDPConnection, target_id: str, session_id: str) -> None:
        conn._on_attached({
            "sessionId": session_id,
            "targetInfo": {
                "targetId": target_id,
                "type": "page",
                "url": "https://example.com",
                "title": "Tab",
                "webSocketDebuggerUrl": "",
            },
        })

    def test_returns_session_for_valid_index(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "s1")
        session = conn.get_session_for_tab(0)
        assert session is not None
        assert session.session_id == "s1"

    def test_returns_none_for_out_of_range(self):
        conn = CDPConnection()
        assert conn.get_session_for_tab(0) is None
        assert conn.get_session_for_tab(-1) is None

    def test_returns_correct_session_by_index(self):
        conn = CDPConnection()
        self._attach(conn, "t1", "s1")
        self._attach(conn, "t2", "s2")
        s0 = conn.get_session_for_tab(0)
        s1 = conn.get_session_for_tab(1)
        assert s0.session_id == "s1"
        assert s1.session_id == "s2"


class TestCDPConnectionGetSessionForTarget:
    @staticmethod
    def _attach(
        conn: CDPConnection,
        target_id: str,
        session_id: str,
        *,
        url: str = "https://example.com",
        title: str = "Tab",
    ) -> None:
        conn._on_attached({
            "sessionId": session_id,
            "targetInfo": {
                "targetId": target_id,
                "type": "page",
                "url": url,
                "title": title,
                "webSocketDebuggerUrl": "",
            },
        })

    def test_target_id_lookup_does_not_confuse_duplicate_urls(self):
        conn = CDPConnection()
        self._attach(conn, "target-a", "session-a", url="https://example.com/same")
        self._attach(conn, "target-b", "session-b", url="https://example.com/same")

        session_a = conn.get_session_for_target("target-a")
        session_b = conn.get_session_for_target("target-b")

        assert session_a is not None
        assert session_b is not None
        assert session_a.session_id == "session-a"
        assert session_b.session_id == "session-b"
        assert session_a.target_id == "target-a"
        assert session_b.target_id == "target-b"

    def test_reattaching_same_target_rebinds_without_duplicate_tab(self):
        conn = CDPConnection()
        self._attach(conn, "target-a", "session-old", title="Before")
        old_session = conn.get_session_for_target("target-a")

        self._attach(conn, "target-a", "session-new", title="After")

        rebound = conn.get_session_for_target("target-a")
        assert rebound is not None
        assert rebound is not old_session
        assert rebound.session_id == "session-new"
        assert len(conn.list_tabs()) == 1
        assert conn.list_tabs()[0].title == "After"
        assert conn.list_tabs()[0].session_id == "session-new"
        assert "session-old" not in conn._sessions

    def test_duplicate_attach_event_preserves_existing_session_object(self):
        conn = CDPConnection()
        self._attach(conn, "target-a", "session-a", title="Before")
        original = conn.get_session_for_target("target-a")

        self._attach(conn, "target-a", "session-a", title="After")

        assert conn.get_session_for_target("target-a") is original
        assert len(conn.list_tabs()) == 1
        assert conn.list_tabs()[0].title == "After"

    def test_unknown_target_returns_none(self):
        conn = CDPConnection()

        assert conn.get_session_for_target("missing") is None


class TestCDPConnectionMessageIdMonotonicity:
    def test_ids_are_strictly_increasing(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            ids = [conn._next_id() for _ in range(10)]
            return ids

        ids = loop.run_until_complete(run())
        loop.close()
        assert ids == list(range(1, 11))

    def test_ids_start_at_one(self):
        conn = CDPConnection()
        first = conn._next_id()
        assert first == 1

    def test_ids_never_repeat(self):
        conn = CDPConnection()
        ids = [conn._next_id() for _ in range(100)]
        assert len(ids) == len(set(ids))


class TestCDPConnectionConnectWithMock:
    """Integration-style tests using a mocked WebSocket."""

    def _make_fake_ws(self, messages: list[str] | None = None) -> FakeWebSocket:
        return FakeWebSocket(messages)

    def test_connect_sets_connected(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            ws = self._make_fake_ws()

            # Inject a response for Target.setAutoAttach
            async def fake_connect(url, **kwargs):
                return ws

            with patch("websockets.connect", new=fake_connect):
                # Patch _send_browser to avoid timing issues in tests
                async def fake_send_browser(method, params=None):
                    return {}

                conn._send_browser = fake_send_browser
                await conn.connect("ws://127.0.0.1:9222/json/version")

            assert conn.is_connected is True
            await conn.disconnect()

        loop.run_until_complete(run())
        loop.close()

    def test_connect_enables_target_discovery_for_metadata_updates(self):
        async def run():
            conn = CDPConnection()
            ws = self._make_fake_ws()
            calls: list[tuple[str, dict | None]] = []

            async def fake_connect(url, **kwargs):
                return ws

            async def fake_send_browser(method, params=None):
                calls.append((method, params))
                return {}

            conn._send_browser = fake_send_browser
            with patch("websockets.connect", new=fake_connect):
                await conn.connect("ws://127.0.0.1:9222/json/version")
            await conn.disconnect()
            return calls

        calls = asyncio.run(run())

        assert ("Target.setDiscoverTargets", {"discover": True}) in calls
        assert (
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            },
        ) in calls

    def test_disconnect_sets_not_connected(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            ws = self._make_fake_ws()

            async def fake_connect(url, **kwargs):
                return ws

            async def fake_send_browser(method, params=None):
                return {}

            conn._send_browser = fake_send_browser
            with patch("websockets.connect", new=fake_connect):
                await conn.connect("ws://127.0.0.1:9222/json/version")
            await conn.disconnect()
            assert conn.is_connected is False

        loop.run_until_complete(run())
        loop.close()

    def test_connect_is_idempotent(self):
        """Calling connect twice should not open a second WebSocket."""
        loop = asyncio.new_event_loop()
        call_count = 0

        async def run():
            nonlocal call_count
            conn = CDPConnection()
            ws = self._make_fake_ws()

            async def fake_connect(url, **kwargs):
                nonlocal call_count
                call_count += 1
                return ws

            async def fake_send_browser(method, params=None):
                return {}

            conn._send_browser = fake_send_browser
            with patch("websockets.connect", new=fake_connect):
                await conn.connect("ws://127.0.0.1:9222/json/version")
                await conn.connect("ws://127.0.0.1:9222/json/version")  # second call

            await conn.disconnect()

        loop.run_until_complete(run())
        loop.close()
        assert call_count == 1

    def test_concurrent_ensure_connected_is_single_flight(self):
        async def run():
            conn = CDPConnection()
            discover_calls = 0
            ws_calls = 0
            connect_calls = 0

            def discover_port():
                nonlocal discover_calls
                discover_calls += 1
                return 9222

            async def get_ws_url(port):
                nonlocal ws_calls
                ws_calls += 1
                await asyncio.sleep(0)
                return "ws://127.0.0.1:9222/devtools/browser/one"

            async def connect_locked(url):
                nonlocal connect_calls
                connect_calls += 1
                await asyncio.sleep(0)
                conn._connected = True

            conn._discover_port = discover_port
            conn._get_browser_ws_url = get_ws_url
            conn._connect_locked = connect_locked

            await asyncio.gather(*(conn.ensure_connected() for _ in range(32)))

            assert discover_calls == 1
            assert ws_calls == 1
            assert connect_calls == 1

        asyncio.run(run())


class TestCDPConnectionLifecycleStress:
    @staticmethod
    def _attach(conn: CDPConnection, target_id: str, session_id: str) -> None:
        conn._on_attached(
            {
                "sessionId": session_id,
                "targetInfo": {
                    "targetId": target_id,
                    "type": "page",
                    "url": f"https://example.test/{target_id}",
                    "title": target_id,
                },
            }
        )

    def test_ten_thousand_unique_attach_detach_cycles_leave_no_state(self):
        conn = CDPConnection()
        conn._generation = 1

        for index in range(10_000):
            target_id = f"target-{index}"
            session_id = f"session-{index}"
            self._attach(conn, target_id, session_id)
            conn._on_detached(
                {"targetId": target_id, "sessionId": session_id}
            )

        assert conn._sessions == {}
        assert conn._tabs == []
        assert conn._tab_by_target == {}
        assert conn._target_waiters == {}

    def test_ten_thousand_rebinds_keep_one_current_target_then_detach_cleanly(self):
        conn = CDPConnection()
        conn._generation = 1

        for index in range(10_000):
            self._attach(conn, "same-target", f"session-{index}")

        assert len(conn._sessions) == 1
        assert len(conn._tabs) == 1
        assert list(conn._tab_by_target) == ["same-target"]

        conn._on_detached(
            {"targetId": "same-target", "sessionId": "session-9999"}
        )

        assert conn._sessions == {}
        assert conn._tabs == []
        assert conn._tab_by_target == {}

    def test_failed_browser_send_reclaims_pending_future(self):
        async def run():
            conn = CDPConnection()
            conn._ws = MagicMock()
            conn._ws.send = AsyncMock(side_effect=ConnectionError("closed"))

            with pytest.raises(ConnectionError, match="closed"):
                await conn._send_browser("Target.setAutoAttach")

            assert conn._browser_pending == {}

        asyncio.run(run())


class TestCDPConnectionReadLoop:
    """Test that the read loop dispatches messages correctly."""

    def test_attached_event_registers_session(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            ws = FakeWebSocket()
            conn._ws = ws
            conn._connected = True

            # Start read loop
            task = asyncio.create_task(conn._read_loop())

            # Push an attachedToTarget event
            ws.push(make_attached_event("t1", "sess-1", url="https://test.com"))

            # Give the loop time to process
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert len(conn._tabs) == 1
            assert conn._tabs[0].url == "https://test.com"
            assert "sess-1" in conn._sessions

        loop.run_until_complete(run())
        loop.close()

    def test_target_info_changed_refreshes_existing_tab_metadata(self):
        async def run():
            conn = CDPConnection()
            conn._on_attached(
                {
                    "sessionId": "sess-1",
                    "targetInfo": {
                        "targetId": "t1",
                        "type": "page",
                        "url": "https://before.test",
                        "title": "Before",
                    },
                }
            )
            ws = FakeWebSocket()
            conn._ws = ws
            conn._connected = True
            task = asyncio.create_task(conn._read_loop())
            ws.push(
                make_event(
                    "Target.targetInfoChanged",
                    {
                        "targetInfo": {
                            "targetId": "t1",
                            "type": "page",
                            "url": "https://after.test",
                            "title": "After",
                        }
                    },
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            await task
            return conn.list_tabs()[0]

        tab = asyncio.run(run())

        assert tab.id == "t1"
        assert tab.url == "https://after.test"
        assert tab.title == "After"
        assert tab.session_id == "sess-1"

    def test_detached_event_removes_session(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            # Pre-populate a tab
            conn._on_attached({
                "sessionId": "sess-2",
                "targetInfo": {
                    "targetId": "t2",
                    "type": "page",
                    "url": "https://bye.com",
                    "title": "Bye",
                    "webSocketDebuggerUrl": "",
                },
            })
            assert len(conn._tabs) == 1

            ws = FakeWebSocket()
            conn._ws = ws
            conn._connected = True
            task = asyncio.create_task(conn._read_loop())

            ws.push(make_detached_event("t2", "sess-2"))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert conn._tabs == []
            assert "sess-2" not in conn._sessions

        loop.run_until_complete(run())
        loop.close()

    def test_session_message_routed_to_correct_session(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            # Manually register a session so we can check routing
            session = CDPSession("sess-A", conn)
            conn._sessions["sess-A"] = session
            received = []
            session.on_event("Page.loadEventFired", lambda p: received.append(p))

            ws = FakeWebSocket()
            conn._ws = ws
            conn._connected = True
            task = asyncio.create_task(conn._read_loop())

            # Push an event for sess-A
            msg = make_event(
                "Page.loadEventFired",
                {"timestamp": 42.0},
                session_id="sess-A",
            )
            ws.push(msg)
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert received == [{"timestamp": 42.0}]

        loop.run_until_complete(run())
        loop.close()

    def test_bad_json_is_skipped(self):
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            ws = FakeWebSocket()
            conn._ws = ws
            conn._connected = True
            task = asyncio.create_task(conn._read_loop())

            ws.push("{not valid json")
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Connection should still be alive (no crash)
            assert conn._connected is True

        loop.run_until_complete(run())
        loop.close()
