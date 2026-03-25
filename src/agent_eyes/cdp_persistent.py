"""Persistent CDP connection — single WebSocket, multiple tab sessions.

Instead of opening/closing a WebSocket per operation (legacy Tier 3 pattern),
this module maintains one persistent WebSocket to the browser's /json/version
endpoint and routes all CDP traffic through it via sessionIds (flat session
model introduced in Chrome M64).

Architecture:
    CDPConnection  — one WebSocket, reads all messages, dispatches by sessionId
    CDPSession     — per-tab logical channel over the shared socket
    ChromeTab      — lightweight tab descriptor (compatible with cdp.ChromeTab)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .platform_utils import discover_cdp_port

logger = logging.getLogger("agent-eyes")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 9222


@dataclass
class ChromeTab:
    """A Chrome browser tab tracked by the persistent connection.

    Compatible interface with cdp.ChromeTab so callers can use either.
    """
    id: str
    title: str
    url: str
    ws_url: str
    session_id: str = ""


class CDPSession:
    """Per-tab CDP session that routes commands through the shared connection.

    Each session corresponds to a Target (browser tab) and is identified by
    a sessionId assigned by Chrome when we attach via Target.attachToTarget.
    All commands are stamped with this sessionId before being written to the
    shared WebSocket by CDPConnection._send_raw().
    """

    def __init__(self, session_id: str, connection: "CDPConnection") -> None:
        self.session_id = session_id
        self._connection = connection
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list] = {}
        self._enabled_domains: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and await its response.

        Returns the 'result' dict from Chrome, or raises RuntimeError on error.
        """
        msg_id = self._connection._next_id()
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future

        await self._connection._send_raw(self.session_id, msg_id, method, params or {})

        try:
            result = await asyncio.wait_for(future, timeout=15.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP command {method} timed out after 15s")

        if "error" in result:
            raise RuntimeError(
                f"CDP error for {method}: {result['error'].get('message', result['error'])}"
            )
        return result.get("result", {})

    async def enable_domain(self, domain: str) -> None:
        """Enable a CDP domain idempotently (cached per session)."""
        if domain in self._enabled_domains:
            return
        await self.send(f"{domain}.enable")
        self._enabled_domains.add(domain)

    def on_event(self, method: str, handler) -> None:
        """Register an event handler for a CDP event method."""
        self._event_handlers.setdefault(method, []).append(handler)

    # ── Internal ──────────────────────────────────────────────────────

    def _on_message(self, msg: dict) -> None:
        """Route an incoming message to the right pending Future or event handler."""
        msg_id = msg.get("id")
        if msg_id is not None:
            # Response to a command we sent
            future = self._pending.pop(msg_id, None)
            if future and not future.done():
                future.set_result(msg)
            return

        # Event
        method = msg.get("method", "")
        handlers = self._event_handlers.get(method, [])
        for handler in handlers:
            try:
                handler(msg.get("params", {}))
            except Exception as exc:
                logger.debug("Event handler error for %s: %s", method, exc)


class CDPConnection:
    """Single persistent WebSocket connection to the Chrome browser.

    Opens exactly one WebSocket to ws://host:port/json/version (the browser
    endpoint), calls Target.setAutoAttach so Chrome notifies us when tabs are
    created/destroyed, and dispatches every incoming message to the right
    CDPSession by sessionId.

    Usage:
        conn = CDPConnection()
        await conn.ensure_connected()
        session = conn.get_session_for_tab(0)
        result = await session.send("Runtime.evaluate", {"expression": "1+1"})
        await conn.disconnect()
    """

    def __init__(self, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> None:
        self._host = host
        self._port = port
        self._discovered_port: int | None = None
        self._ws = None
        self._connected = False
        self._msg_id = 0
        self._id_lock = asyncio.Lock()
        self._sessions: dict[str, CDPSession] = {}   # sessionId → CDPSession
        self._tabs: list[ChromeTab] = []              # ordered list of tabs
        self._tab_by_target: dict[str, ChromeTab] = {}  # targetId → ChromeTab
        self._read_task: asyncio.Task | None = None
        self._browser_pending: dict[int, asyncio.Future] = {}  # browser-level commands

    # ── Properties ────────────────────────────────────────────────────

    @property
    def active_port(self) -> int:
        """Return the port currently in use."""
        return self._discovered_port or self._port

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self, browser_ws_url: str) -> None:
        """Connect to the browser-level WebSocket endpoint.

        Args:
            browser_ws_url: ws://host:port/json/version WebSocket URL.
        """
        import websockets

        if self._connected:
            return

        logger.info("CDPConnection: connecting to %s", browser_ws_url)
        self._ws = await websockets.connect(browser_ws_url)  # type: ignore[attr-defined]
        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())

        # Ask Chrome to auto-attach to all existing and future tabs.
        # flatten=True uses the flat session model (sessionId-based routing).
        await self._send_browser("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })
        logger.info("CDPConnection: ready (auto-attach enabled)")

    async def disconnect(self) -> None:
        """Close the WebSocket and cancel the read loop."""
        self._connected = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._sessions.clear()
        self._tabs.clear()
        self._tab_by_target.clear()
        logger.info("CDPConnection: disconnected")

    async def ensure_connected(self) -> None:
        """Connect if not already connected, auto-discovering the CDP port.

        Raises RuntimeError if Chrome is not reachable.
        """
        if self._connected:
            return

        port = self._discover_port()
        if port is None:
            raise RuntimeError(
                "Chrome remote debugging not available. "
                "Start Chrome with --remote-debugging-port=9222"
            )

        browser_ws = await self._get_browser_ws_url(port)
        if not browser_ws:
            raise RuntimeError(
                f"Could not get browser WebSocket URL from port {port}. "
                "Is Chrome running with --remote-debugging-port?"
            )

        self._discovered_port = port
        await self.connect(browser_ws)

    # ── Tab / session access ──────────────────────────────────────────

    def list_tabs(self) -> list[ChromeTab]:
        """Return tracked tabs without making any HTTP or WS call."""
        return list(self._tabs)

    def get_session_for_tab(self, tab_index: int) -> CDPSession | None:
        """Get the CDPSession for the tab at the given index.

        Returns None if the index is out of range or the tab has no session.
        """
        if tab_index < 0 or tab_index >= len(self._tabs):
            return None
        tab = self._tabs[tab_index]
        return self._sessions.get(tab.session_id)

    # ── Internal: message ID ──────────────────────────────────────────

    async def _next_id(self) -> int:
        """Return a monotonically increasing message ID (thread-safe)."""
        async with self._id_lock:
            self._msg_id += 1
            return self._msg_id

    # ── Internal: sending ─────────────────────────────────────────────

    async def _send_raw(
        self, session_id: str, msg_id: int, method: str, params: dict
    ) -> None:
        """Write a CDP message to the WebSocket, stamped with sessionId."""
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send(json.dumps(msg))

    async def _send_browser(self, method: str, params: dict | None = None) -> dict:
        """Send a browser-level CDP command (no sessionId) and await response."""
        msg_id = await self._next_id()
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._browser_pending[msg_id] = future

        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
        except asyncio.TimeoutError:
            self._browser_pending.pop(msg_id, None)
            raise RuntimeError(f"Browser command {method} timed out")

        return result.get("result", {})

    # ── Internal: read loop ───────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Continuously read messages from the WebSocket and dispatch them."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("CDPConnection: bad JSON from Chrome")
                    continue

                session_id = msg.get("sessionId", "")
                method = msg.get("method", "")

                # Browser-level events (no sessionId)
                if not session_id:
                    if method == "Target.attachedToTarget":
                        self._on_attached(msg.get("params", {}))
                    elif method == "Target.detachedFromTarget":
                        self._on_detached(msg.get("params", {}))
                    else:
                        # Browser-level command response
                        msg_id = msg.get("id")
                        if msg_id is not None:
                            future = self._browser_pending.pop(msg_id, None)
                            if future and not future.done():
                                future.set_result(msg)
                    continue

                # Session-level message — route to the right CDPSession
                session = self._sessions.get(session_id)
                if session:
                    session._on_message(msg)
                else:
                    logger.debug(
                        "CDPConnection: message for unknown session %s", session_id
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("CDPConnection: read loop error: %s", exc)
            self._connected = False

    # ── Internal: target lifecycle ────────────────────────────────────

    def _on_attached(self, params: dict) -> None:
        """Handle Target.attachedToTarget — register a new CDPSession."""
        session_id: str = params.get("sessionId", "")
        target_info: dict = params.get("targetInfo", {})
        target_id: str = target_info.get("targetId", "")
        target_type: str = target_info.get("type", "")

        if not session_id:
            return

        # Only track page targets (tabs)
        if target_type != "page":
            logger.debug(
                "CDPConnection: ignoring non-page target %s (type=%s)",
                target_id,
                target_type,
            )
            return

        session = CDPSession(session_id, self)
        self._sessions[session_id] = session

        tab = ChromeTab(
            id=target_id,
            title=target_info.get("title", ""),
            url=target_info.get("url", ""),
            ws_url=target_info.get("webSocketDebuggerUrl", ""),
            session_id=session_id,
        )
        self._tabs.append(tab)
        self._tab_by_target[target_id] = tab

        logger.info(
            "CDPConnection: attached to tab [%d] %s session=%s",
            len(self._tabs) - 1,
            tab.url,
            session_id[:8],
        )

    def _on_detached(self, params: dict) -> None:
        """Handle Target.detachedFromTarget — clean up session and tab."""
        session_id: str = params.get("sessionId", "")
        target_id: str = params.get("targetId", "")

        # Remove session
        self._sessions.pop(session_id, None)

        # Remove tab
        tab = self._tab_by_target.pop(target_id, None)
        if tab:
            try:
                self._tabs.remove(tab)
            except ValueError:
                pass
            logger.info("CDPConnection: detached from tab %s", target_id[:8])

    # ── Internal: port discovery ──────────────────────────────────────

    def _discover_port(self) -> int | None:
        """Try to find Chrome's CDP port."""
        discovered = discover_cdp_port()
        if discovered:
            return discovered
        # Try the default port synchronously via a quick socket check
        import socket
        try:
            with socket.create_connection((self._host, self._port), timeout=1):
                return self._port
        except OSError:
            return None

    async def _get_browser_ws_url(self, port: int) -> str | None:
        """Fetch the browser WebSocket URL from /json/version HTTP endpoint."""
        import urllib.request
        try:
            url = f"http://{self._host}:{port}/json/version"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                return data.get("webSocketDebuggerUrl")
        except Exception as exc:
            logger.debug("CDPConnection: /json/version failed: %s", exc)
            return None
