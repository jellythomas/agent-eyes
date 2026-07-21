from __future__ import annotations

import asyncio

import pytest
import websockets

from agent_eyes.cdp import CDPClient, CDP_MAX_MESSAGE_BYTES, _connect_websocket
from agent_eyes.cdp_persistent import CDPConnection


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CDPClient(host="example.com"),
        lambda: CDPClient(host="localhost@attacker.example"),
        lambda: CDPConnection(host="example.com"),
        lambda: CDPConnection(host="localhost/path"),
    ],
)
def test_cdp_clients_reject_non_loopback_or_malformed_hosts(factory):
    with pytest.raises(ValueError, match="loopback"):
        factory()


@pytest.mark.parametrize("port", [0, 65_536, -1, True, "9222"])
def test_cdp_clients_reject_invalid_ports(port):
    with pytest.raises(ValueError, match="port"):
        CDPClient(port=port)

    with pytest.raises(ValueError, match="port"):
        CDPConnection(port=port)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "::1"])
def test_cdp_clients_accept_loopback_hosts(host):
    assert CDPClient(host=host).host
    assert CDPConnection(host=host)._host


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.com/devtools/browser/one",
        "wss://localhost@attacker.example/devtools/browser/one",
        "file:///tmp/fake-cdp",
    ],
)
def test_cdp_websocket_transport_rejects_non_loopback_urls(url):
    with pytest.raises(ValueError, match="loopback WebSocket"):
        _connect_websocket(url)


def test_cdp_transport_accepts_valid_response_larger_than_websockets_default():
    payload_size = 1_100_000

    async def run() -> int:
        async def send_payload(websocket) -> None:
            await websocket.send("x" * payload_size)

        async with websockets.serve(
            send_payload,
            "127.0.0.1",
            0,
            compression=None,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            async with _connect_websocket(f"ws://127.0.0.1:{port}") as websocket:
                message = await websocket.recv()
                return len(message)

    assert asyncio.run(run()) == payload_size


def test_cdp_transport_rejects_response_above_shared_ceiling():
    async def run() -> None:
        async def send_payload(websocket) -> None:
            await websocket.send("x" * (CDP_MAX_MESSAGE_BYTES + 1))

        async with websockets.serve(
            send_payload,
            "127.0.0.1",
            0,
            compression=None,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            async with _connect_websocket(f"ws://127.0.0.1:{port}") as websocket:
                with pytest.raises(websockets.exceptions.ConnectionClosedError) as error:
                    await websocket.recv()

        assert error.value.sent is not None
        assert error.value.sent.code == 1009

    asyncio.run(run())
