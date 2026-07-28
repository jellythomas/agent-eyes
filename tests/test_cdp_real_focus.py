from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.cdp import CDPClient, ChromeTab, _connect_websocket


def _chrome_binary() -> str:
    candidates = (
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("a local Chrome/Chromium executable is required")


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_for_tab(client: CDPClient) -> ChromeTab:
    deadline = asyncio.get_running_loop().time() + 20.0
    while asyncio.get_running_loop().time() < deadline:
        tabs = await client.list_tabs()
        if tabs:
            return tabs[0]
        await asyncio.sleep(0.05)
    raise AssertionError("headless Chrome did not expose a CDP page target")


async def _install_focus_spoof(client: CDPClient, tab: ChromeTab) -> int:
    async with _connect_websocket(tab.ws_url) as ws:
        await client._send(ws, "DOM.enable")
        await client._send(
            ws,
            "Runtime.evaluate",
            {
                "expression": """
                    document.body.innerHTML = `
                        <input id="target" aria-label="Target">
                        <input id="other" aria-label="Other">
                    `;
                    const target = document.getElementById('target');
                    const other = document.getElementById('other');
                    window.__agentEyesNativeActiveElement =
                        Object.getOwnPropertyDescriptor(
                            Document.prototype,
                            'activeElement'
                        ).get;
                    const nativeFocus = HTMLElement.prototype.focus;
                    HTMLElement.prototype.focus = function() {
                        nativeFocus.call(other);
                    };
                    Object.defineProperty(Document.prototype, 'activeElement', {
                        configurable: true,
                        get() { return target; },
                    });
                """,
            },
        )
        remote = await client._send(
            ws,
            "Runtime.evaluate",
            {"expression": "document.getElementById('target')"},
        )
        object_id = remote.get("result", {}).get("objectId")
        assert object_id
        description = await client._send(
            ws,
            "DOM.describeNode",
            {"objectId": object_id, "depth": 0},
        )
        backend_id = description.get("node", {}).get("backendNodeId")
        assert isinstance(backend_id, int) and not isinstance(backend_id, bool)
        return backend_id


async def _read_actual_state(client: CDPClient, tab: ChromeTab) -> dict:
    async with _connect_websocket(tab.ws_url) as ws:
        result = await client._send(
            ws,
            "Runtime.evaluate",
            {
                "expression": """
                    JSON.stringify({
                        actual: window.__agentEyesNativeActiveElement.call(document).id,
                        target: document.getElementById('target').value,
                        other: document.getElementById('other').value,
                    })
                """,
                "returnByValue": True,
            },
        )
    return json.loads(result["result"]["value"])


def test_protocol_focus_resists_page_focus_and_active_element_spoof():
    async def run(port: int) -> None:
        client = CDPClient(host="127.0.0.1", port=port)
        tab = await _wait_for_tab(client)
        backend_id = await _install_focus_spoof(client, tab)
        element = UIElement(
            id=1,
            role="textbox",
            name="Target",
            source="cdp",
            platform_ref=backend_id,
        )
        original_send = client._send
        input_dispatches = 0

        async def counted_send(ws, method, params=None, **kwargs):
            nonlocal input_dispatches
            if method == "Input.insertText":
                input_dispatches += 1
            return await original_send(ws, method, params, **kwargs)

        client._send = counted_send
        applied = await client.type_text(
            tab,
            backend_id,
            "private-secret",
            expected_element=element,
        )
        actual = await _read_actual_state(client, tab)

        assert applied is True
        assert input_dispatches == 1
        assert actual == {
            "actual": "target",
            "target": "private-secret",
            "other": "",
        }

    chrome = _chrome_binary()
    port = _unused_loopback_port()
    with tempfile.TemporaryDirectory(
        prefix="agent-eyes-focus-",
        ignore_cleanup_errors=True,
    ) as profile:
        process = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            asyncio.run(run(port))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
