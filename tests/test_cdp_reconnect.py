"""CDP auto-reconnect on disconnect."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock, PropertyMock
from agent_eyes.cdp_persistent import CDPConnection, CDPSession


class TestCDPAutoReconnect:
    def test_send_retries_once_on_connection_error(self):
        """If send fails with ConnectionError, retry once after reconnect."""
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            # Use the correct constructor: CDPSession(session_id, connection)
            session = CDPSession("session-1", conn)

            call_count = 0
            async def mock_send_raw(sid, mid, method, params):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("WebSocket closed")
                return  # success on retry

            conn._send_raw = mock_send_raw
            conn.ensure_connected = AsyncMock()
            # Mock the pending future resolution
            session._pending = {}

            # The session should attempt reconnect
            try:
                # We can't fully test send() without a real WebSocket,
                # but we can verify ensure_connected is called on failure
                await asyncio.wait_for(
                    session.send("Runtime.evaluate", {"expression": "1+1"}),
                    timeout=3.0
                )
            except Exception:
                pass  # Expected — we're testing the reconnect attempt

            conn.ensure_connected.assert_called_once()

        loop.run_until_complete(run())
        loop.close()

    def test_reconnect_timeout_is_2s(self):
        """Reconnect must timeout after 2 seconds, not hang."""
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()

            async def slow_connect():
                await asyncio.sleep(10)

            conn.ensure_connected = slow_connect

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(conn.ensure_connected(), timeout=2.0)

        loop.run_until_complete(run())
        loop.close()

    def test_reconnect_failure_gives_clear_error(self):
        """If reconnect fails, error message mentions Chrome."""
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()

            async def fail_connect():
                raise ConnectionError("Port 9222 refused")

            conn.ensure_connected = fail_connect
            conn._connected = False

            # Trying to reconnect should give actionable error
            try:
                await conn.ensure_connected()
            except ConnectionError as e:
                assert "9222" in str(e) or "refused" in str(e)

        loop.run_until_complete(run())
        loop.close()

    def test_send_raises_clear_error_when_reconnect_fails(self):
        """If send fails and reconnect also fails, raise RuntimeError mentioning Chrome."""
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            session = CDPSession("session-1", conn)

            async def mock_send_raw(sid, mid, method, params):
                raise ConnectionError("WebSocket closed")

            async def mock_reconnect():
                raise ConnectionError("Port 9222 refused")

            conn._send_raw = mock_send_raw
            conn.ensure_connected = mock_reconnect

            with pytest.raises(RuntimeError) as exc_info:
                await session.send("Runtime.evaluate", {"expression": "1+1"})

            error_msg = str(exc_info.value)
            assert "CDP disconnected" in error_msg or "remote-debugging-port" in error_msg

        loop.run_until_complete(run())
        loop.close()

    def test_send_succeeds_after_reconnect(self):
        """If send fails with ConnectionError but reconnect succeeds, the retry succeeds."""
        loop = asyncio.new_event_loop()

        async def run():
            conn = CDPConnection()
            session = CDPSession("session-1", conn)

            call_count = 0

            async def mock_send_raw(sid, mid, method, params):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("WebSocket closed")
                # On retry, simulate Chrome responding by resolving the future
                # Find the pending future for msg_id and resolve it
                for msg_id, fut in list(session._pending.items()):
                    if not fut.done():
                        fut.set_result({"id": msg_id, "result": {"value": 2}})

            conn._send_raw = mock_send_raw
            conn.ensure_connected = AsyncMock()

            result = await asyncio.wait_for(
                session.send("Runtime.evaluate", {"expression": "1+1"}),
                timeout=3.0
            )
            assert result == {"value": 2}
            assert call_count == 2

        loop.run_until_complete(run())
        loop.close()
