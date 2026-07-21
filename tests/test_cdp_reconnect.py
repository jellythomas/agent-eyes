"""Deterministic reconnect and replay-safety tests for persistent CDP sessions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_eyes.cdp_persistent import CDPConnection, CDPSession
from agent_eyes.operation import OperationError, OperationErrorCode


def _attach(
    conn: CDPConnection,
    target_id: str,
    session_id: str,
    *,
    url: str = "https://example.com",
) -> CDPSession:
    conn._on_attached({
        "sessionId": session_id,
        "targetInfo": {
            "targetId": target_id,
            "type": "page",
            "url": url,
            "title": target_id,
            "webSocketDebuggerUrl": "",
        },
    })
    session = conn.get_session_for_target(target_id)
    assert session is not None
    return session


def test_non_idempotent_mutation_is_never_replayed_after_disconnect():
    async def run() -> None:
        conn = CDPConnection()
        session = _attach(conn, "target-1", "session-old")
        send_raw = AsyncMock(side_effect=ConnectionError("WebSocket closed"))
        conn._send_raw = send_raw
        conn.reconnect = AsyncMock()

        with pytest.raises(RuntimeError, match="outcome is unknown.*not retried"):
            await session.send("Page.navigate", {"url": "https://new.example"})

        send_raw.assert_awaited_once()
        conn.reconnect.assert_not_awaited()

    asyncio.run(run())


def test_explicit_idempotent_read_rebinds_by_target_after_disconnect():
    async def run() -> None:
        conn = CDPConnection()
        stale_session = _attach(conn, "target-1", "session-old")
        sent_session_ids: list[str] = []

        async def send_raw(
            session_id: str,
            msg_id: int,
            method: str,
            params: dict,
        ) -> None:
            sent_session_ids.append(session_id)
            if session_id == "session-old":
                raise ConnectionError("WebSocket closed")
            current = conn._sessions[session_id]
            current._on_message({"id": msg_id, "result": {"value": 2}})

        async def reconnect(failed_generation: int) -> None:
            assert failed_generation == stale_session.generation
            _attach(conn, "target-1", "session-new")

        conn._send_raw = send_raw
        conn.reconnect = AsyncMock(side_effect=reconnect)

        result = await stale_session.send(
            "Runtime.evaluate",
            {"expression": "1+1"},
            idempotent=True,
        )

        assert result == {"value": 2}
        assert sent_session_ids == ["session-old", "session-new"]
        conn.reconnect.assert_awaited_once_with(stale_session.generation)
        assert conn.get_session_for_target("target-1").session_id == "session-new"

    asyncio.run(run())


def test_stale_session_object_forwards_later_commands_to_current_target_binding():
    async def run() -> None:
        conn = CDPConnection()
        stale_session = _attach(conn, "target-1", "session-old")
        sent_session_ids: list[str] = []

        async def send_raw(
            session_id: str,
            msg_id: int,
            method: str,
            params: dict,
        ) -> None:
            sent_session_ids.append(session_id)
            current = conn._sessions[session_id]
            current._on_message({"id": msg_id, "result": {"ok": True}})

        _attach(conn, "target-1", "session-new")
        conn._send_raw = send_raw
        conn.reconnect = AsyncMock()

        result = await stale_session.send("Runtime.evaluate", idempotent=True)

        assert result == {"ok": True}
        assert sent_session_ids == ["session-new"]
        conn.reconnect.assert_not_awaited()

    asyncio.run(run())


def test_stale_session_object_rejects_mutation_before_new_generation_dispatch():
    async def run() -> None:
        conn = CDPConnection()
        stale_session = _attach(conn, "target-1", "session-old")
        current_session = _attach(conn, "target-1", "session-new")
        conn._send_raw = AsyncMock()

        with pytest.raises(OperationError) as exc_info:
            await stale_session.send(
                "Input.insertText",
                {"text": "must-not-move-generations"},
            )

        assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT
        assert conn.get_session_for_target("target-1") is current_session
        conn._send_raw.assert_not_awaited()

    asyncio.run(run())


def test_idempotent_read_never_retries_stale_session_when_target_is_missing():
    async def run() -> None:
        conn = CDPConnection()
        stale_session = _attach(conn, "target-1", "session-old")
        send_raw = AsyncMock(side_effect=ConnectionError("WebSocket closed"))
        conn._send_raw = send_raw
        conn.reconnect = AsyncMock()
        conn.wait_for_session_for_target = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="target target-1 did not reattach"):
            await stale_session.send(
                "Runtime.evaluate",
                {"expression": "document.title"},
                idempotent=True,
            )

        send_raw.assert_awaited_once()
        conn.wait_for_session_for_target.assert_awaited_once_with("target-1")

    asyncio.run(run())


def test_idempotent_retry_requires_canonical_target_identity():
    async def run() -> None:
        conn = CDPConnection()
        session = CDPSession("session-without-target", conn)
        conn._send_raw = AsyncMock(side_effect=ConnectionError("WebSocket closed"))
        conn.reconnect = AsyncMock()

        with pytest.raises(RuntimeError, match="has no target ID.*not retried"):
            await session.send("Runtime.evaluate", idempotent=True)

        conn.reconnect.assert_not_awaited()

    asyncio.run(run())


def test_reconnect_failure_is_reported_without_swallowing_exception():
    async def run() -> None:
        conn = CDPConnection()
        session = _attach(conn, "target-1", "session-old")
        conn._send_raw = AsyncMock(side_effect=ConnectionError("WebSocket closed"))
        conn.reconnect = AsyncMock(side_effect=ConnectionError("Port 9222 refused"))

        with pytest.raises(RuntimeError, match="Reconnect failed: Port 9222 refused"):
            await session.send("Runtime.evaluate", idempotent=True)

    asyncio.run(run())


def test_reconnect_timeout_uses_immediate_fake_not_wall_clock_sleep():
    async def run() -> None:
        conn = CDPConnection()
        session = _attach(conn, "target-1", "session-old")
        conn._send_raw = AsyncMock(side_effect=ConnectionError("WebSocket closed"))
        conn.reconnect = AsyncMock(side_effect=asyncio.TimeoutError)

        with pytest.raises(RuntimeError, match="Reconnect timed out"):
            await session.send("Runtime.evaluate", idempotent=True)

    asyncio.run(run())


def test_disconnect_while_awaiting_response_does_not_replay_mutation():
    async def run() -> None:
        conn = CDPConnection()
        session = _attach(conn, "target-1", "session-old")
        send_count = 0

        async def send_raw(
            session_id: str,
            msg_id: int,
            method: str,
            params: dict,
        ) -> None:
            nonlocal send_count
            send_count += 1
            session._pending[msg_id].set_exception(
                RuntimeError("CDPConnection: read loop terminated")
            )

        conn._send_raw = send_raw
        conn.reconnect = AsyncMock()

        with pytest.raises(RuntimeError, match="outcome is unknown.*not retried"):
            await session.send("Input.insertText", {"text": "hello"})

        assert send_count == 1
        conn.reconnect.assert_not_awaited()

    asyncio.run(run())


def test_concurrent_reconnect_keeps_the_single_fresh_transport_open():
    async def run() -> None:
        class ProbeWebSocket:
            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1

        conn = CDPConnection()
        failed_generation = 1
        failed_transport = ProbeWebSocket()
        reader_cancelled = asyncio.Event()
        release_reader = asyncio.Event()
        replacements: list[ProbeWebSocket] = []

        async def blocked_reader() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                reader_cancelled.set()
                await release_reader.wait()

        async def install_replacement() -> None:
            replacement = ProbeWebSocket()
            replacements.append(replacement)
            conn._ws = replacement
            conn._connected = True
            conn._generation += 1

        conn._connected = True
        conn._generation = failed_generation
        conn._ws = failed_transport
        conn._read_task = asyncio.create_task(blocked_reader())

        # Patch both paths so this regression also fails against the former
        # reconnect implementation, which re-entered public ensure_connected().
        conn.ensure_connected = install_replacement
        conn._ensure_connected_locked = install_replacement

        first = asyncio.create_task(conn.reconnect())
        await reader_cancelled.wait()
        second = asyncio.create_task(conn.reconnect())
        await asyncio.sleep(0)
        release_reader.set()

        await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

        assert len(replacements) == 1
        assert conn._ws is replacements[0]
        assert replacements[0].close_calls == 0
        assert failed_transport.close_calls == 1
        assert conn._generation == failed_generation + 1

    asyncio.run(run())
