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
from dataclasses import dataclass
from typing import Any

from .cdp import (
    _KEY_MAP,
    _MAX_PIERCED_DOM_NODES,
    _PIERCED_CONTROL_SELECTOR,
    _PIERCED_SELECTOR_DEPTH,
    _cdp_http_get,
    _connect_websocket,
    _flatten_dom_nodes,
    _validate_cdp_endpoint,
    _validate_cdp_websocket_url,
)
from .platform_utils import discover_cdp_port
from .operation import OperationError, OperationErrorCode

logger = logging.getLogger("agent-eyes")

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 9222
_COMMAND_TIMEOUT_SECONDS = 15.0
_RECONNECT_TIMEOUT_SECONDS = 2.0
_REATTACH_TIMEOUT_SECONDS = 2.0
_DOM_DESCRIBE_CONCURRENCY = 8


def _is_connection_error(exc: Exception) -> bool:
    """Return whether an exception means the command transport was lost."""
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    try:
        import websockets.exceptions

        if isinstance(exc, websockets.exceptions.ConnectionClosed):
            return True
    except ImportError:
        pass
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in ("closed", "disconnected", "read loop terminated")
    )


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

    def __init__(
        self,
        session_id: str,
        connection: "CDPConnection",
        *,
        target_id: str = "",
        generation: int = 0,
        dom_describe_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.session_id = session_id
        self.target_id = target_id
        self.generation = generation
        self._connection = connection
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list] = {}
        self._enabled_domains: set[str] = set()
        self._dom_describe_semaphore = (
            dom_describe_semaphore
            if dom_describe_semaphore is not None
            else asyncio.Semaphore(_DOM_DESCRIBE_CONCURRENCY)
        )

    # ── Public API ────────────────────────────────────────────────────

    @property
    def has_pending_commands(self) -> bool:
        """Return whether a sent command still awaits transport settlement."""
        return bool(self._pending)

    async def wait_until_idle(self) -> None:
        """Wait until every command already sent on this session settles."""
        while self._pending:
            pending = tuple(self._pending.values())
            await asyncio.gather(
                *(asyncio.shield(future) for future in pending),
                return_exceptions=True,
            )

    async def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        idempotent: bool = False,
    ) -> dict:
        """Send a CDP command and await its response.

        Commands are never replayed after a disconnect unless the caller marks
        the operation explicitly idempotent. An idempotent retry is rebound by
        the page's canonical target ID; stale session IDs are never reused.
        """
        if self.target_id:
            current = self._connection.get_session_for_target(self.target_id)
            if current is not None and current is not self:
                if not idempotent:
                    raise OperationError(
                        OperationErrorCode.STALE_SNAPSHOT,
                        f"CDP target {self.target_id} was rebound; {method} was not sent",
                    )
                return await current.send(
                    method,
                    params,
                    idempotent=idempotent,
                )

        try:
            return await self._send_once(method, params or {})
        except Exception as exc:
            if not _is_connection_error(exc):
                raise

            self._connection._mark_disconnected(self.generation)
            if not idempotent:
                raise RuntimeError(
                    f"CDP disconnected during {method}; command outcome is unknown "
                    "and it was not retried."
                ) from exc
            if not self.target_id:
                raise RuntimeError(
                    f"CDP session {self.session_id} has no target ID; idempotent "
                    f"command {method} was not retried."
                ) from exc

            try:
                await asyncio.wait_for(
                    self._connection.reconnect(self.generation),
                    timeout=_RECONNECT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as reconnect_exc:
                raise RuntimeError(
                    f"CDP disconnected during {method}. Reconnect timed out after "
                    f"{_RECONNECT_TIMEOUT_SECONDS:g}s."
                ) from reconnect_exc
            except Exception as reconnect_exc:
                raise RuntimeError(
                    f"CDP disconnected during {method}. "
                    f"Reconnect failed: {reconnect_exc}"
                ) from reconnect_exc

            rebound = self._connection.get_session_for_target(self.target_id)
            if rebound is None or rebound is self:
                rebound = await self._connection.wait_for_session_for_target(
                    self.target_id
                )
            if rebound is None or rebound is self:
                raise RuntimeError(
                    f"CDP reconnected during {method}, but target {self.target_id} "
                    "did not reattach; the stale session was not retried."
                )

            try:
                return await rebound._send_once(method, params or {})
            except Exception as retry_exc:
                if _is_connection_error(retry_exc):
                    raise RuntimeError(
                        f"CDP disconnected again while retrying idempotent command "
                        f"{method} for target {self.target_id}."
                    ) from retry_exc
                raise

    async def _send_once(self, method: str, params: dict) -> dict:
        """Send exactly once on this session without reconnecting or replaying."""
        msg_id = self._connection._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._connection._send_raw(
                self.session_id,
                msg_id,
                method,
                params,
            )
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"CDP command {method} timed out after "
                f"{_COMMAND_TIMEOUT_SECONDS:g}s"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if future.done():
                self._pending.pop(msg_id, None)
            raise

        if "error" in result:
            raise RuntimeError(
                f"CDP error for {method}: {result['error'].get('message', result['error'])}"
            )
        return result.get("result", {})

    async def enable_domain(self, domain: str) -> None:
        """Enable a CDP domain idempotently (cached per session)."""
        if domain in self._enabled_domains:
            return
        await self.send(f"{domain}.enable", idempotent=True)
        self._enabled_domains.add(domain)

    def on_event(self, method: str, handler) -> None:
        """Register an event handler for a CDP event method."""
        self._event_handlers.setdefault(method, []).append(handler)

    def off_event(self, method: str, handler) -> None:
        """Remove one event handler without disturbing other subscribers."""
        handlers = self._event_handlers.get(method)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._event_handlers.pop(method, None)

    async def run_until_event(
        self,
        methods: str | tuple[str, ...],
        action,
        *,
        timeout: float,
    ) -> tuple[Any, dict]:
        """Subscribe first, run one action, then await the first matching event."""
        event_methods = (methods,) if isinstance(methods, str) else methods
        loop = asyncio.get_running_loop()
        event: asyncio.Future[dict] = loop.create_future()

        def receive(params: dict) -> None:
            if not event.done():
                event.set_result(params)

        for method in event_methods:
            self.on_event(method, receive)
        try:
            action_result = await action()
            params = await asyncio.wait_for(event, timeout=max(0.0, timeout))
            return action_result, params
        finally:
            for method in event_methods:
                self.off_event(method, receive)
            if not event.done():
                event.cancel()

    async def wait_for_ax_node(
        self,
        role: str = "",
        name: str = "",
        timeout: float = 5.0,
    ) -> dict | None:
        """Wait for an accessibility match, re-querying only after CDP events.

        The query is scoped to the page's root backend node as required by
        Accessibility.queryAXTree. Event subscriptions are always removed on
        success, timeout, cancellation, or protocol failure.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)

        async def bounded(call):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(call(), timeout=remaining)

        try:
            await bounded(lambda: self.enable_domain("Accessibility"))
        except asyncio.TimeoutError:
            return None
        changed = asyncio.Event()

        def wake(_params: dict) -> None:
            changed.set()

        event_methods = (
            "Accessibility.nodesUpdated",
            "Accessibility.loadComplete",
        )
        for method in event_methods:
            self.on_event(method, wake)

        async def query() -> dict | None:
            root_result = await self.send("Accessibility.getRootAXNode")
            root = root_result.get("node", {})
            params: dict[str, Any] = {}
            backend_node_id = root.get("backendDOMNodeId")
            node_id = root.get("nodeId")
            if backend_node_id is not None:
                params["backendNodeId"] = backend_node_id
            elif node_id:
                params["nodeId"] = node_id
            else:
                raise RuntimeError("Accessibility root node has no queryable identifier")
            if role:
                params["role"] = role
            if name:
                params["accessibleName"] = name
            result = await self.send("Accessibility.queryAXTree", params)
            nodes = result.get("nodes", [])
            return nodes[0] if nodes else None

        try:
            while True:
                changed.clear()
                try:
                    match = await bounded(query)
                except asyncio.TimeoutError:
                    return None
                if match is not None:
                    return match
                if changed.is_set():
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
        finally:
            for method in event_methods:
                self.off_event(method, wake)

    async def search_dom(
        self,
        query: str,
        *,
        max_results: int = 50,
        include_user_agent_shadow_dom: bool = True,
    ) -> list[dict]:
        """Return exact DOM search matches with canonical backend node IDs.

        Chrome interprets ``query`` as text, a CSS selector, or XPath.  The
        caller value is transported as a protocol parameter, never generated
        source.  Search state is discarded on every exit path.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("DOM search query must be a non-empty string")
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or max_results < 1
            or max_results > 500
        ):
            raise ValueError("max_results must be an integer between 1 and 500")
        if not isinstance(include_user_agent_shadow_dom, bool):
            raise ValueError("include_user_agent_shadow_dom must be a boolean")

        await self.enable_domain("DOM")
        search = await self.send(
            "DOM.performSearch",
            {
                "query": query,
                "includeUserAgentShadowDOM": include_user_agent_shadow_dom,
            },
            idempotent=True,
        )
        search_id = search.get("searchId")
        if not isinstance(search_id, str) or not search_id:
            raise RuntimeError("DOM.performSearch returned no search ID")

        try:
            result_count = search.get("resultCount", 0)
            if isinstance(result_count, bool) or not isinstance(result_count, int):
                raise RuntimeError("DOM.performSearch returned an invalid result count")
            result_count = max(0, result_count)
            to_index = min(result_count, max_results)
            if to_index == 0:
                return []

            results = await self.send(
                "DOM.getSearchResults",
                {
                    "searchId": search_id,
                    "fromIndex": 0,
                    "toIndex": to_index,
                },
                idempotent=True,
            )
            node_ids = results.get("nodeIds", [])
            if not isinstance(node_ids, list):
                raise RuntimeError("DOM.getSearchResults returned invalid node IDs")

            valid_node_ids = [
                node_id
                for node_id in node_ids[:max_results]
                if isinstance(node_id, int) and not isinstance(node_id, bool)
            ]
            descriptions = await self._describe_search_nodes(valid_node_ids)
            return [
                description["node"]
                for description in descriptions
                if isinstance(description.get("node"), dict)
            ]
        finally:
            try:
                await self.send(
                    "DOM.discardSearchResults",
                    {"searchId": search_id},
                )
            except Exception as exc:
                logger.debug(
                    "Could not discard DOM search state (exception_type=%s)",
                    type(exc).__name__,
                )

    async def _describe_search_nodes(self, node_ids: list[int]) -> list[dict]:
        """Describe search results with bounded pressure on the shared socket."""
        if not node_ids:
            return []

        descriptions: list[dict | None] = [None] * len(node_ids)
        pending = iter(enumerate(node_ids))

        async def worker() -> None:
            for index, node_id in pending:
                async with self._dom_describe_semaphore:
                    descriptions[index] = await self.send(
                        "DOM.describeNode",
                        {"nodeId": node_id, "depth": 0},
                        idempotent=True,
                    )

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(_DOM_DESCRIBE_CONCURRENCY, len(node_ids)))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        return [
            description
            for description in descriptions
            if description is not None
        ]

    async def get_pierced_dom(
        self,
        *,
        max_nodes: int = _MAX_PIERCED_DOM_NODES,
    ) -> list[dict]:
        """Return bounded named controls from light and pierced shadow DOM."""
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or max_nodes < 1
            or max_nodes > _MAX_PIERCED_DOM_NODES
        ):
            raise ValueError(
                "max_nodes must be an integer between 1 and "
                f"{_MAX_PIERCED_DOM_NODES}"
            )
        return await self.search_dom(
            _PIERCED_CONTROL_SELECTOR,
            max_results=max_nodes,
        )

    async def pierce_selector(
        self,
        selector: str,
        *,
        max_hosts: int = 20,
        max_nodes: int = 500,
    ) -> list[dict]:
        """Return bounded pierced subtrees rooted at exact selector matches."""
        if (
            isinstance(max_hosts, bool)
            or not isinstance(max_hosts, int)
            or not 1 <= max_hosts <= 50
        ):
            raise ValueError("max_hosts must be an integer between 1 and 50")
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or not 1 <= max_nodes <= 500
        ):
            raise ValueError("max_nodes must be an integer between 1 and 500")
        hosts = await self.search_dom(selector, max_results=max_hosts)
        nodes: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for host in hosts:
            backend_id = host.get("backendNodeId")
            node_id = host.get("nodeId")
            params: dict[str, Any] = {
                "depth": _PIERCED_SELECTOR_DEPTH,
                "pierce": True,
            }
            if isinstance(backend_id, int) and not isinstance(backend_id, bool):
                params["backendNodeId"] = backend_id
            elif isinstance(node_id, int) and not isinstance(node_id, bool):
                params["nodeId"] = node_id
            else:
                continue
            description = await self.send(
                "DOM.describeNode",
                params,
                idempotent=True,
            )
            root = description.get("node")
            if not isinstance(root, dict):
                continue
            for node in _flatten_dom_nodes(root, max_nodes=max_nodes - len(nodes)):
                canonical = node.get("backendNodeId")
                frontend = node.get("nodeId")
                if isinstance(canonical, int) and not isinstance(canonical, bool):
                    identity = ("backend", canonical)
                elif isinstance(frontend, int) and not isinstance(frontend, bool):
                    identity = ("node", frontend)
                else:
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                nodes.append(node)
                if len(nodes) >= max_nodes:
                    return nodes
        return nodes

    async def press_key(
        self,
        key: str,
        modifiers: list[str] | None = None,
    ) -> None:
        """Dispatch one key-down/key-up pair without replay semantics."""
        modifier_flags = 0
        for modifier in modifiers or []:
            normalized = modifier.casefold()
            if normalized in {"alt", "option"}:
                modifier_flags |= 1
            elif normalized in {"ctrl", "control"}:
                modifier_flags |= 2
            elif normalized in {"meta", "cmd", "command"}:
                modifier_flags |= 4
            elif normalized == "shift":
                modifier_flags |= 8
        descriptor = _KEY_MAP.get(key.casefold()) or {
            "key": key,
            "code": f"Key{key.upper()}" if len(key) == 1 else key,
            "keyCode": ord(key.upper()) if len(key) == 1 else 0,
            "text": key if len(key) == 1 else "",
        }
        down: dict[str, Any] = {
            "type": "keyDown",
            "key": descriptor["key"],
            "code": descriptor.get("code", ""),
            "windowsVirtualKeyCode": descriptor.get("keyCode", 0),
            "nativeVirtualKeyCode": descriptor.get("keyCode", 0),
            "modifiers": modifier_flags,
        }
        if descriptor.get("text"):
            down["text"] = descriptor["text"]
        await self.send("Input.dispatchKeyEvent", down)
        await self.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": descriptor["key"],
                "code": descriptor.get("code", ""),
                "windowsVirtualKeyCode": descriptor.get("keyCode", 0),
                "nativeVirtualKeyCode": descriptor.get("keyCode", 0),
                "modifiers": modifier_flags,
            },
        )

    async def scroll(
        self,
        x: int,
        y: int,
        delta_x: int,
        delta_y: int,
    ) -> None:
        """Dispatch one protocol wheel event."""
        await self.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
        )

    async def drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        *,
        steps: int = 10,
    ) -> None:
        """Dispatch one bounded drag sequence without fixed sleeps."""
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
            raise ValueError("steps must be an integer between 1 and 100")
        await self.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": from_x,
                "y": from_y,
                "button": "left",
                "clickCount": 1,
            },
        )
        for index in range(1, steps + 1):
            fraction = index / steps
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": int(from_x + (to_x - from_x) * fraction),
                    "y": int(from_y + (to_y - from_y) * fraction),
                    "button": "left",
                },
            )
        await self.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": to_x,
                "y": to_y,
                "button": "left",
                "clickCount": 1,
            },
        )

    async def handle_dialog(self, accept: bool, prompt_text: str = "") -> None:
        """Resolve one JavaScript dialog on this exact target."""
        params: dict[str, Any] = {"accept": accept}
        if prompt_text:
            params["promptText"] = prompt_text
        await self.send("Page.handleJavaScriptDialog", params)

    async def set_file_input(
        self,
        backend_node_id: int,
        files: list[str],
    ) -> None:
        """Set files on one exact backend node."""
        await self.send(
            "DOM.setFileInputFiles",
            {"files": files, "backendNodeId": backend_node_id},
        )

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
                logger.debug(
                    "CDP event handler failed "
                    "(method_length=%d handler_count=%d exception_type=%s)",
                    len(method) if isinstance(method, str) else 0,
                    len(handlers),
                    type(exc).__name__,
                )


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
        self._host, self._port = _validate_cdp_endpoint(host, port)
        self._discovered_port: int | None = None
        self._ws = None
        self._connected = False
        self._generation = 0
        self._msg_id = 0
        self._sessions: dict[str, CDPSession] = {}   # sessionId → CDPSession
        self._tabs: list[ChromeTab] = []              # ordered list of tabs
        self._tab_by_target: dict[str, ChromeTab] = {}  # targetId → ChromeTab
        self._target_waiters: dict[str, list[asyncio.Future]] = {}
        self._read_task: asyncio.Task | None = None
        self._browser_pending: dict[int, asyncio.Future] = {}  # browser-level commands
        self._connect_lock = asyncio.Lock()
        self._dom_describe_semaphore = asyncio.Semaphore(
            _DOM_DESCRIBE_CONCURRENCY
        )

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
        async with self._connect_lock:
            await self._connect_locked(browser_ws_url)

    async def _connect_locked(self, browser_ws_url: str) -> None:
        """Establish one transport while ``_connect_lock`` is held."""
        if self._connected:
            return

        logger.info("CDPConnection: connecting to browser endpoint")
        websocket = await _connect_websocket(browser_ws_url)
        self._ws = websocket
        self._generation += 1
        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())

        try:
            # Metadata changes are emitted only while target discovery is
            # enabled. Keep the in-memory tab inventory live across same-tab
            # navigations instead of waiting for a detach/reattach cycle.
            await self._send_browser(
                "Target.setDiscoverTargets",
                {"discover": True},
            )
            # Ask Chrome to auto-attach to all existing and future tabs.
            # flatten=True uses the flat session model (sessionId routing).
            await self._send_browser("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            })
        except BaseException:
            self._connected = False
            read_task = self._read_task
            self._read_task = None
            if read_task is not None and not read_task.done():
                read_task.cancel()
                try:
                    await read_task
                except asyncio.CancelledError:
                    pass
            self._ws = None
            try:
                await websocket.close()
            except Exception as exc:
                logger.debug(
                    "CDPConnection: startup transport close failed "
                    "(exception_type=%s)",
                    type(exc).__name__,
                )
            raise
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
        for waiters in self._target_waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
        self._target_waiters.clear()
        logger.info("CDPConnection: disconnected")

    async def reconnect(self, failed_generation: int | None = None) -> None:
        """Replace one failed transport while preserving target identities.

        Concurrent callers that observed the same failed generation share the
        replacement installed by the first successful caller. The teardown and
        connect transaction stays under ``_connect_lock`` so a late caller
        cannot close that fresh transport.
        """
        reconnect_generation = (
            self._generation if failed_generation is None else failed_generation
        )

        async with self._connect_lock:
            if self._generation != reconnect_generation:
                return

            self._mark_disconnected(reconnect_generation)
            read_task = self._read_task
            self._read_task = None
            if (
                read_task is not None
                and read_task is not asyncio.current_task()
                and not read_task.done()
            ):
                read_task.cancel()
                try:
                    await read_task
                except asyncio.CancelledError:
                    pass

            websocket = self._ws
            self._ws = None
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception as exc:
                    logger.debug(
                        "CDPConnection: closing failed transport "
                        "(exception_type=%s)",
                        type(exc).__name__,
                    )

            self._retire_transport_generation(reconnect_generation)
            await self._ensure_connected_locked()

    def _mark_disconnected(self, failed_generation: int | None = None) -> None:
        """Mark only the transport generation that a caller observed failed."""
        if failed_generation is None or failed_generation == self._generation:
            self._connected = False

    async def ensure_connected(self) -> None:
        """Connect if not already connected, auto-discovering the CDP port.

        Raises RuntimeError if Chrome is not reachable.
        """
        if self._connected:
            return
        async with self._connect_lock:
            await self._ensure_connected_locked()

    async def _ensure_connected_locked(self) -> None:
        """Connect once while ``_connect_lock`` is already held."""
        if self._connected:
            return

        port = await asyncio.to_thread(self._discover_port)
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
        await self._connect_locked(browser_ws)

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
        return self.get_session_for_target(tab.id)

    def get_session_for_target(self, target_id: str) -> CDPSession | None:
        """Return the current-generation session for one canonical target ID."""
        tab = self._tab_by_target.get(target_id)
        if tab is None:
            return None
        session = self._sessions.get(tab.session_id)
        if session is None:
            return None
        if session.target_id != target_id:
            return None
        if session.generation != self._generation:
            return None
        return session

    async def wait_for_session_for_target(
        self,
        target_id: str,
        timeout: float = _REATTACH_TIMEOUT_SECONDS,
    ) -> CDPSession | None:
        """Wait for auto-attach to publish a current session for target_id."""
        session = self.get_session_for_target(target_id)
        if session is not None:
            return session

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        waiters = self._target_waiters.setdefault(target_id, [])
        waiters.append(future)
        try:
            return await asyncio.wait_for(future, timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return None
        finally:
            current_waiters = self._target_waiters.get(target_id)
            if current_waiters is not None:
                try:
                    current_waiters.remove(future)
                except ValueError:
                    pass
                if not current_waiters:
                    self._target_waiters.pop(target_id, None)

    async def close_target(self, target_id: str) -> bool:
        """Request closure of one canonical target exactly once."""
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("target_id is required")
        result = await self._send_browser(
            "Target.closeTarget",
            {"targetId": target_id},
        )
        return result.get("success") is True

    # ── Internal: message ID ──────────────────────────────────────────

    def _next_id(self) -> int:
        """Return a monotonically increasing message ID.

        Asyncio is single-threaded — no lock needed for a simple counter.
        """
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
        msg_id = self._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._browser_pending[msg_id] = future

        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        try:
            await self._ws.send(json.dumps(msg))
            result = await asyncio.wait_for(future, timeout=10.0)
        except asyncio.TimeoutError:
            self._browser_pending.pop(msg_id, None)
            raise RuntimeError(f"Browser command {method} timed out")
        except BaseException:
            self._browser_pending.pop(msg_id, None)
            if not future.done():
                future.cancel()
            raise

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
                    elif method == "Target.targetInfoChanged":
                        self._on_target_info_changed(msg.get("params", {}))
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
                        "CDPConnection: message for unknown session "
                        "(session_id_length=%d)",
                        len(session_id) if isinstance(session_id, str) else 0,
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "CDPConnection: read loop error (exception_type=%s)",
                type(exc).__name__,
            )
            self._connected = False
        finally:
            # Cancel all pending futures so callers don't hang
            loop_err = RuntimeError("CDPConnection: read loop terminated")
            for session in self._sessions.values():
                for fut in session._pending.values():
                    if not fut.done():
                        fut.set_exception(loop_err)
                session._pending.clear()
            for fut in self._browser_pending.values():
                if not fut.done():
                    fut.set_exception(loop_err)
            self._browser_pending.clear()

    # ── Internal: target lifecycle ────────────────────────────────────

    def _on_attached(self, params: dict) -> None:
        """Handle Target.attachedToTarget — register a new CDPSession."""
        session_id: str = params.get("sessionId", "")
        target_info: dict = params.get("targetInfo", {})
        target_id: str = target_info.get("targetId", "")
        target_type: str = target_info.get("type", "")

        if not session_id or not target_id:
            return

        # Only track page targets (tabs)
        if target_type != "page":
            logger.debug(
                "CDPConnection: ignoring non-page target "
                "(target_id_length=%d target_type_length=%d)",
                len(target_id) if isinstance(target_id, str) else 0,
                len(target_type) if isinstance(target_type, str) else 0,
            )
            return

        existing_tab = self._tab_by_target.get(target_id)
        session = None
        if existing_tab is not None and existing_tab.session_id == session_id:
            candidate = self._sessions.get(session_id)
            if (
                candidate is not None
                and candidate.target_id == target_id
                and candidate.generation == self._generation
            ):
                session = candidate

        if session is None:
            if existing_tab is not None:
                self._retire_session(
                    existing_tab.session_id,
                    "CDP target was rebound to a new session",
                )
            session = CDPSession(
                session_id,
                self,
                target_id=target_id,
                generation=self._generation,
                dom_describe_semaphore=self._dom_describe_semaphore,
            )
            self._sessions[session_id] = session

        if existing_tab is None:
            tab = ChromeTab(
                id=target_id,
                title=target_info.get("title", ""),
                url=target_info.get("url", ""),
                ws_url=target_info.get("webSocketDebuggerUrl", ""),
                session_id=session_id,
            )
            self._tabs.append(tab)
            self._tab_by_target[target_id] = tab
        else:
            tab = existing_tab
            tab.title = target_info.get("title", "")
            tab.url = target_info.get("url", "")
            tab.ws_url = target_info.get("webSocketDebuggerUrl", "")
            tab.session_id = session_id

        for waiter in self._target_waiters.pop(target_id, []):
            if not waiter.done():
                waiter.set_result(session)

        logger.info(
            "CDPConnection: attached page target "
            "(tab_index=%d target_id_length=%d session_id_length=%d)",
            len(self._tabs) - 1,
            len(target_id),
            len(session_id),
        )

    def _on_target_info_changed(self, params: dict) -> None:
        """Refresh mutable metadata for one already attached page target."""
        target_info = params.get("targetInfo", {})
        if not isinstance(target_info, dict):
            return
        target_id = target_info.get("targetId", "")
        if not isinstance(target_id, str) or not target_id:
            return
        tab = self._tab_by_target.get(target_id)
        if tab is None or target_info.get("type", "page") != "page":
            return
        title = target_info.get("title")
        url = target_info.get("url")
        ws_url = target_info.get("webSocketDebuggerUrl")
        if isinstance(title, str):
            tab.title = title
        if isinstance(url, str):
            tab.url = url
        if isinstance(ws_url, str):
            tab.ws_url = ws_url

    def _on_detached(self, params: dict) -> None:
        """Handle Target.detachedFromTarget — clean up session and tab."""
        session_id: str = params.get("sessionId", "")
        target_id: str = params.get("targetId", "")

        session = self._sessions.get(session_id)
        if not target_id and session is not None:
            target_id = session.target_id
        self._retire_session(
            session_id,
            "Tab was closed while commands were pending",
        )

        # Ignore a late detach for a session that has already been replaced.
        tab = self._tab_by_target.get(target_id)
        if tab is not None and tab.session_id == session_id:
            self._tab_by_target.pop(target_id, None)
            try:
                self._tabs.remove(tab)
            except ValueError:
                pass
            logger.info(
                "CDPConnection: detached from tab "
                "(remaining_tabs=%d target_id_length=%d)",
                len(self._tabs),
                len(target_id),
            )

    def _retire_session(self, session_id: str, reason: str) -> None:
        """Fail pending commands and remove exactly one session binding."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        error = RuntimeError(reason)
        for future in session._pending.values():
            if not future.done():
                future.set_exception(error)
        session._pending.clear()
        session._event_handlers.clear()
        session._enabled_domains.clear()

    def _retire_transport_generation(self, generation: int) -> None:
        """Remove session and tab state owned by a retired transport."""
        retired_session_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.generation <= generation
        ]
        for session_id in retired_session_ids:
            self._retire_session(
                session_id,
                "CDP transport was retired during reconnect",
            )

        for target_id, tab in list(self._tab_by_target.items()):
            session = self._sessions.get(tab.session_id)
            if session is None or session.target_id != target_id:
                self._tab_by_target.pop(target_id, None)

        self._tabs[:] = [
            tab
            for tab in self._tabs
            if self._tab_by_target.get(tab.id) is tab
        ]

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
        def _fetch_sync() -> str | None:
            try:
                status, body = _cdp_http_get(
                    self._host,
                    port,
                    "/json/version",
                    timeout=3,
                )
                if status != 200:
                    return None
                data = json.loads(body)
                ws_url = data.get("webSocketDebuggerUrl")
                if ws_url:
                    _validate_cdp_websocket_url(ws_url)
                return ws_url
            except Exception as exc:
                logger.debug(
                    "CDPConnection: /json/version failed (exception_type=%s)",
                    type(exc).__name__,
                )
                return None

        return await asyncio.get_running_loop().run_in_executor(None, _fetch_sync)
