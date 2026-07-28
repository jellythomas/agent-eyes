"""Chrome DevTools Protocol client for enriched accessibility trees.

Cross-platform: works on macOS, Windows, and Linux — anywhere Chrome runs.
Gets accessibility trees enriched with bounded DOM targeting metadata and
bounding boxes, giving AI agents spatial awareness without screenshots.
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import http.client
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from .adapters.base import UIElement
from .cdp_runtime import (
    CLICK_FUNCTION,
    RuntimeActionStatus,
    ax_element_has_exact_focus,
    ax_element_semantics_match,
    parse_runtime_action_status,
    require_empty_command_result,
)
from .js_bridge import _tree_node_is_secure
from .platform_utils import discover_cdp_port

logger = logging.getLogger("agent-eyes")

MAX_EVAL_RESULT_LEN = 10_000
_MAX_PIERCED_DOM_NODES = 500
_PIERCED_SELECTOR_DEPTH = 8
_MAX_BUFFERED_EVENTS = 256
_MAX_AX_CONVERSION_NODES = 5_000
_MAX_AX_CONVERSION_BYTES = 4 * 1024 * 1024
_MAX_SECURE_DOM_CANDIDATES = 2_048
_MAX_SECURE_DOM_MATCHES = 10_000
_TEXT_VALUE_ROLES = frozenset({"textbox", "searchbox", "textarea"})
_SECURE_CONTROL_SELECTOR = (
    'input[type="password" i],'
    '[autocomplete~="current-password" i],'
    '[autocomplete~="new-password" i]'
)
_SECURE_TEXT_STYLES = (
    {"name": "-webkit-text-security", "value": "circle"},
    {"name": "-webkit-text-security", "value": "disc"},
    {"name": "-webkit-text-security", "value": "square"},
)
CDP_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
CDP_HTTP_MAX_BYTES = 4 * 1024 * 1024

_PIERCED_CONTROL_SELECTOR = ",".join(
    (
        "a[aria-label]",
        "a[title]",
        "button[aria-label]",
        "button[title]",
        "button[value]",
        "input[aria-label]",
        "input[title]",
        "input[placeholder]",
        "input[value]",
        "select[aria-label]",
        "select[title]",
        "textarea[aria-label]",
        "textarea[title]",
        "textarea[placeholder]",
        "img[aria-label]",
        "img[title]",
        "img[alt]",
        "[role][aria-label]",
        "[role][title]",
        "[role][placeholder]",
        "[role][value]",
        "[tabindex][aria-label]",
        "[tabindex][title]",
        "[contenteditable][aria-label]",
        "[contenteditable][title]",
    )
)


def _validate_cdp_endpoint(host: str, port: int) -> tuple[str, int]:
    """Return a canonical loopback-only CDP endpoint."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError("CDP host must be a loopback address")

    candidate = host.strip()
    if candidate.casefold() == "localhost":
        normalized_host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError("CDP host must be a loopback address") from exc
        if not address.is_loopback:
            raise ValueError("CDP host must be a loopback address")
        normalized_host = address.compressed

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ValueError("CDP port must be an integer from 1 through 65535")
    return normalized_host, port


def _cdp_http_authority(host: str, port: int) -> str:
    """Format one validated CDP endpoint for an HTTP URL."""
    host, port = _validate_cdp_endpoint(host, port)
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{rendered_host}:{port}"


def _validate_cdp_websocket_url(url: str) -> str:
    """Reject any CDP WebSocket URL outside the local machine."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.hostname is None or parsed.port is None:
            raise ValueError
        _validate_cdp_endpoint(parsed.hostname, parsed.port)
    except (TypeError, ValueError) as exc:
        raise ValueError("CDP requires a loopback WebSocket URL") from exc
    return url


def _cdp_http_get(
    host: str,
    port: int,
    path: str,
    *,
    timeout: float,
) -> tuple[int, bytes]:
    """Issue one bounded, redirect-free request to a local CDP endpoint."""
    host, port = _validate_cdp_endpoint(host, port)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\r" in path
        or "\n" in path
    ):
        raise ValueError("CDP HTTP path must use origin form")

    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > CDP_HTTP_MAX_BYTES:
            raise ValueError("CDP HTTP response exceeded the size limit")
        body = response.read(CDP_HTTP_MAX_BYTES + 1)
        if len(body) > CDP_HTTP_MAX_BYTES:
            raise ValueError("CDP HTTP response exceeded the size limit")
        return response.status, body
    finally:
        connection.close()


def _connect_websocket(url: str):
    """Open one CDP transport with the shared, finite response ceiling."""
    import websockets

    return websockets.connect(
        _validate_cdp_websocket_url(url),
        max_size=CDP_MAX_MESSAGE_BYTES,
    )


class CDPDocumentChangedError(RuntimeError):
    """The target document no longer matches the observed snapshot."""


class CDPMutationOutcomeUnknown(RuntimeError):
    """A mutation was dispatched but its response was not confirmed."""


class CDPFocusMismatchError(RuntimeError):
    """The exact target did not own document focus, so input was not sent."""


def _document_revision(frame_result: dict, document_result: dict) -> int:
    """Build a deterministic revision from the top frame and DOM root."""
    frame = frame_result.get("frameTree", {}).get("frame", {})
    frame_identity = frame.get("loaderId") or frame.get("id")
    root_identity = document_result.get("root", {}).get("backendNodeId")
    if (
        not isinstance(frame_identity, str)
        or not frame_identity
        or isinstance(root_identity, bool)
        or not isinstance(root_identity, int)
        or root_identity <= 0
    ):
        raise RuntimeError("CDP page did not expose a document revision")
    identity = f"{frame_identity}:{root_identity}"
    digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _flatten_dom_nodes(
    root: dict,
    *,
    max_nodes: int = _MAX_PIERCED_DOM_NODES,
) -> list[dict]:
    """Flatten a nested ``DOM.getDocument`` result in stable tree order."""
    if not isinstance(root, dict):
        raise TypeError("DOM root must be a dictionary")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a positive integer")

    flattened: list[dict] = []
    stack = [root]
    seen: set[tuple[str, int]] = set()
    while stack and len(flattened) < max_nodes:
        node = stack.pop()
        node_id = node.get("nodeId")
        identity = (
            ("node", node_id)
            if isinstance(node_id, int) and not isinstance(node_id, bool)
            else ("object", id(node))
        )
        if identity in seen:
            continue
        seen.add(identity)
        flattened.append(node)

        descendants: list[dict] = []
        for field in ("children", "shadowRoots", "pseudoElements"):
            values = node.get(field, [])
            if isinstance(values, list):
                descendants.extend(value for value in values if isinstance(value, dict))
        for field in (
            "contentDocument",
            "templateContent",
            "importedDocument",
        ):
            value = node.get(field)
            if isinstance(value, dict):
                descendants.append(value)
        stack.extend(reversed(descendants))
    return flattened


def _bounded_ax_nodes(
    nodes: list[dict],
    *,
    max_nodes: int = _MAX_AX_CONVERSION_NODES,
    max_bytes: int = _MAX_AX_CONVERSION_BYTES,
) -> tuple[list[dict], bool]:
    """Bound AX conversion work by count and compact-JSON byte size."""
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a positive integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    selected: list[dict] = []
    total_bytes = 0
    truncated = False
    for node in nodes:
        if len(selected) >= max_nodes:
            truncated = True
            break
        if not isinstance(node, dict):
            truncated = True
            break
        try:
            node_bytes = len(
                json.dumps(
                    node,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
            )
        except (TypeError, ValueError):
            truncated = True
            break
        if node_bytes > max_bytes - total_bytes:
            truncated = True
            break
        selected.append(node)
        total_bytes += node_bytes

    if len(selected) < len(nodes):
        truncated = True
    return selected, truncated


# Key name → CDP key descriptor mapping for special keys
_KEY_MAP: dict[str, dict[str, Any]] = {
    "enter": {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "keyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "keyCode": 27},
    "esc": {"key": "Escape", "code": "Escape", "keyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "keyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "keyCode": 46},
    "space": {"key": " ", "code": "Space", "keyCode": 32, "text": " "},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "keyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "keyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "keyCode": 39},
    "home": {"key": "Home", "code": "Home", "keyCode": 36},
    "end": {"key": "End", "code": "End", "keyCode": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "keyCode": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "keyCode": 34},
    "f1": {"key": "F1", "code": "F1", "keyCode": 112},
    "f2": {"key": "F2", "code": "F2", "keyCode": 113},
    "f3": {"key": "F3", "code": "F3", "keyCode": 114},
    "f4": {"key": "F4", "code": "F4", "keyCode": 115},
    "f5": {"key": "F5", "code": "F5", "keyCode": 116},
    "f6": {"key": "F6", "code": "F6", "keyCode": 117},
    "f7": {"key": "F7", "code": "F7", "keyCode": 118},
    "f8": {"key": "F8", "code": "F8", "keyCode": 119},
    "f9": {"key": "F9", "code": "F9", "keyCode": 120},
    "f10": {"key": "F10", "code": "F10", "keyCode": 121},
    "f11": {"key": "F11", "code": "F11", "keyCode": 122},
    "f12": {"key": "F12", "code": "F12", "keyCode": 123},
}


@dataclass
class ChromeTab:
    """A Chrome browser tab."""

    id: str
    title: str
    url: str
    ws_url: str


@dataclass(frozen=True, slots=True)
class SecureDOMMetadata:
    """Security classification for AX nodes obtained without reading values."""

    secure_backend_node_ids: frozenset[int] = frozenset()
    complete: bool = False


class CDPClient:
    """Lightweight Chrome DevTools Protocol client using websockets.

    Auto-discovers Chrome's CDP port from DevToolsActivePort file,
    falling back to the specified port (default 9222).
    """

    def __init__(self, host: str = "localhost", port: int = 9222):
        self.host, self.port = _validate_cdp_endpoint(host, port)
        self._discovered_port: int | None = None
        self._msg_id = 0
        self._id_counter = 0

    @property
    def active_port(self) -> int:
        """Return the discovered port or the configured default."""
        return self._discovered_port or self.port

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def reset_ids(self):
        self._id_counter = 0

    async def is_available(self) -> bool:
        """Check if Chrome is running with remote debugging enabled.

        Tries in order:
        1. Auto-discover port from DevToolsActivePort file
        2. Try the configured default port (9222)
        """
        # Try auto-discovery first
        discovered = discover_cdp_port()
        if discovered:
            if await self._check_port(discovered):
                self._discovered_port = discovered
                logger.info("CDP auto-discovered on port %d", discovered)
                return True

        # Try default port
        if await self._check_port(self.port):
            self._discovered_port = None
            return True

        return False

    async def _check_port(self, port: int) -> bool:
        """Check if CDP is available on a specific port."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{_cdp_http_authority(self.host, port)}/json/version",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    return resp.status == 200
        except Exception:
            # Fallback: try with urllib (no aiohttp dependency)
            try:
                return await self._check_port_urllib(port)
            except Exception:
                return False

    async def _check_port_urllib(self, port: int) -> bool:
        """Fallback availability check without aiohttp."""

        def _check_sync() -> bool:
            try:
                status, _body = _cdp_http_get(
                    self.host,
                    port,
                    "/json/version",
                    timeout=2,
                )
                return status == 200
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _check_sync)

    async def list_tabs(self) -> list[ChromeTab]:
        """List all Chrome tabs."""

        def _list_sync() -> list[ChromeTab]:
            try:
                status, body = _cdp_http_get(
                    self.host,
                    self.active_port,
                    "/json",
                    timeout=5,
                )
                if status != 200:
                    return []
                data = json.loads(body)

                tabs = []
                for item in data:
                    if item.get("type") != "page":
                        continue
                    ws_url = item.get("webSocketDebuggerUrl", "")
                    if not ws_url:
                        continue
                    # Validate WebSocket URL points to localhost
                    try:
                        _validate_cdp_websocket_url(ws_url)
                    except ValueError:
                        logger.warning(
                            "CDPClient: rejected non-loopback WS endpoint",
                        )
                        continue
                    tabs.append(
                        ChromeTab(
                            id=item.get("id", ""),
                            title=item.get("title", ""),
                            url=item.get("url", ""),
                            ws_url=ws_url,
                        )
                    )
                return tabs
            except Exception:
                return []

        return await asyncio.get_running_loop().run_in_executor(None, _list_sync)

    async def collect_secure_dom_metadata(
        self,
        send: Callable[[str, dict], Awaitable[dict]],
        nodes: list[dict],
    ) -> SecureDOMMetadata:
        """Classify value-bearing text controls with bounded batched queries.

        Candidate backend IDs are mapped in one protocol call. A bounded CSS
        search identifies password/autocomplete controls, while one computed-
        style query covers stylesheet-applied text security. No whole-document
        pierced tree or per-element attribute/style round trips are required.

        Any unsupported or malformed protocol response returns incomplete
        metadata. Callers must treat incomplete metadata as a reason to redact
        all text-control values.
        """
        if not isinstance(nodes, list):
            return SecureDOMMetadata()
        nodes, _nodes_truncated = _bounded_ax_nodes(nodes)
        if not nodes:
            return SecureDOMMetadata()

        candidate_backend_ids: list[int] = []
        seen_backend_ids: set[int] = set()
        has_unclassifiable_value = False
        for node in nodes:
            role_value = node.get("role", {})
            role = (
                role_value.get("value", "")
                if isinstance(role_value, dict)
                else role_value
            )
            value_payload = node.get("value", {})
            value = (
                value_payload.get("value", "")
                if isinstance(value_payload, dict)
                else value_payload
            )
            backend_id = node.get("backendDOMNodeId")
            if str(role).casefold() not in _TEXT_VALUE_ROLES or not value:
                continue
            if (
                isinstance(backend_id, bool)
                or not isinstance(backend_id, int)
                or backend_id <= 0
            ):
                has_unclassifiable_value = True
                continue
            if backend_id in seen_backend_ids:
                continue
            seen_backend_ids.add(backend_id)
            candidate_backend_ids.append(backend_id)

        if has_unclassifiable_value:
            return SecureDOMMetadata()
        if not candidate_backend_ids:
            return SecureDOMMetadata(complete=True)
        if len(candidate_backend_ids) > _MAX_SECURE_DOM_CANDIDATES:
            return SecureDOMMetadata()

        search_id = ""
        try:
            document = await send(
                "DOM.getDocument",
                {"depth": 0, "pierce": False},
            )
            root = document.get("root")
            if not isinstance(root, dict):
                return SecureDOMMetadata()
            root_node_id = root.get("nodeId")
            if (
                isinstance(root_node_id, bool)
                or not isinstance(root_node_id, int)
                or root_node_id <= 0
            ):
                return SecureDOMMetadata()

            pushed = await send(
                "DOM.pushNodesByBackendIdsToFrontend",
                {"backendNodeIds": candidate_backend_ids},
            )
            frontend_node_ids = pushed.get("nodeIds")
            if (
                not isinstance(frontend_node_ids, list)
                or len(frontend_node_ids) != len(candidate_backend_ids)
                or any(
                    isinstance(node_id, bool)
                    or not isinstance(node_id, int)
                    or node_id <= 0
                    for node_id in frontend_node_ids
                )
            ):
                return SecureDOMMetadata()

            frontend_by_backend = dict(
                zip(candidate_backend_ids, frontend_node_ids, strict=True)
            )
            search = await send(
                "DOM.performSearch",
                {
                    "query": _SECURE_CONTROL_SELECTOR,
                    "includeUserAgentShadowDOM": True,
                },
            )
            search_id = search.get("searchId", "")
            result_count = search.get("resultCount", 0)
            if (
                not isinstance(search_id, str)
                or not search_id
                or isinstance(result_count, bool)
                or not isinstance(result_count, int)
                or not 0 <= result_count <= _MAX_SECURE_DOM_MATCHES
            ):
                return SecureDOMMetadata()

            secure_node_ids: list[int] = []
            if result_count:
                search_results = await send(
                    "DOM.getSearchResults",
                    {
                        "searchId": search_id,
                        "fromIndex": 0,
                        "toIndex": result_count,
                    },
                )
                secure_node_ids = search_results.get("nodeIds", [])
                if (
                    not isinstance(secure_node_ids, list)
                    or len(secure_node_ids) != result_count
                    or any(
                        isinstance(node_id, bool)
                        or not isinstance(node_id, int)
                        or node_id <= 0
                        for node_id in secure_node_ids
                    )
                ):
                    return SecureDOMMetadata()

            styled = await send(
                "DOM.getNodesForSubtreeByStyle",
                {
                    "nodeId": root_node_id,
                    "computedStyles": list(_SECURE_TEXT_STYLES),
                    "pierce": True,
                },
            )
            styled_node_ids = styled.get("nodeIds")
            if (
                not isinstance(styled_node_ids, list)
                or len(styled_node_ids) > _MAX_SECURE_DOM_MATCHES
                or any(
                    isinstance(node_id, bool)
                    or not isinstance(node_id, int)
                    or node_id <= 0
                    for node_id in styled_node_ids
                )
            ):
                return SecureDOMMetadata()

            secure_node_id_set = set(secure_node_ids)
            secure_node_id_set.update(styled_node_ids)
            return SecureDOMMetadata(
                secure_backend_node_ids=frozenset(
                    backend_id
                    for backend_id, frontend_id in frontend_by_backend.items()
                    if frontend_id in secure_node_id_set
                ),
                complete=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "CDP secure-field classification failed (%s)",
                type(exc).__name__,
            )
            return SecureDOMMetadata()
        finally:
            if search_id:
                try:
                    await send(
                        "DOM.discardSearchResults",
                        {"searchId": search_id},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(
                        "CDP secure-field search cleanup failed (%s)",
                        type(exc).__name__,
                    )

    async def get_accessibility_tree(
        self, tab: ChromeTab, max_depth: int = 5, enrich: bool = True
    ) -> UIElement | None:
        """Get the accessibility tree of a Chrome tab as a UIElement tree.

        When ``enrich=True``, interactive elements get bounding boxes through
        one bounded DOM command per element.
        """
        self.reset_ids()
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                # Enable accessibility domain
                await self._send(ws, "Accessibility.enable")

                # Get full AX tree
                result = await self._send(
                    ws,
                    "Accessibility.getFullAXTree",
                    {"depth": max_depth},
                )

                raw_nodes = result.get("nodes", [])
                if not isinstance(raw_nodes, list) or not raw_nodes:
                    return None
                nodes, nodes_truncated = _bounded_ax_nodes(raw_nodes)
                if not nodes:
                    return UIElement(
                        id=self._next_id(),
                        role="document",
                        name="Accessibility tree conversion limit reached",
                        states=["truncated"],
                        source="cdp",
                    )

                secure_metadata = await self.collect_secure_dom_metadata(
                    lambda method, params: self._send(ws, method, params),
                    nodes,
                )
                tree = self._build_tree(
                    nodes,
                    secure_metadata=secure_metadata,
                )
                if tree is not None and nodes_truncated:
                    tree.states.append("truncated")

                # Enrich interactive elements with layout + visual data
                if enrich and tree:
                    await self._enrich_tree(ws, tree)

                return tree
        except Exception as exc:
            logger.debug(
                "Legacy accessibility tree failed (exception_type=%s)",
                type(exc).__name__,
            )
            return None

    # Interactive roles worth enriching with layout/visual data
    _ENRICH_ROLES = frozenset(
        {
            "button",
            "link",
            "textbox",
            "combobox",
            "searchbox",
            "checkbox",
            "radio",
            "switch",
            "slider",
            "spinbutton",
            "menuitem",
            "tab",
            "heading",
            "img",
            "banner",
            "navigation",
            "main",
            "complementary",
            "contentinfo",
            "form",
        }
    )

    async def _enrich_tree(self, ws, element: UIElement, limit: int = 60) -> int:
        """Walk tree and enrich interactive elements with bounds + visual info.

        Enriches up to `limit` elements to keep CDP calls bounded.
        Returns number of elements enriched.

        Legacy CDP has a single receive stream, so enrichment keeps only the
        highest-value one-round-trip field: the element's box model. Visual
        style summaries cost two extra commands per element and aren't needed
        for reliable targeting.
        """
        # Enable the one required domain once before walking the tree.
        await self._send(ws, "DOM.enable")
        return await self._enrich_subtree(ws, element, limit)

    async def _enrich_subtree(self, ws, element: UIElement, limit: int) -> int:
        """Recursive helper for _enrich_tree (domains already enabled)."""
        if limit <= 0:
            return 0
        enriched = 0

        if element.role in self._ENRICH_ROLES and element.platform_ref:
            try:
                box = await self._get_box_model(ws, element.platform_ref)
                if box:
                    element.bounds = box

                enriched += 1
            except Exception:
                pass  # Non-critical — tree still works without enrichment

        for child in element.children:
            if enriched >= limit:
                break
            enriched += await self._enrich_subtree(ws, child, limit - enriched)

        return enriched

    async def _get_box_model(
        self, ws, backend_node_id: int
    ) -> tuple[int, int, int, int] | None:
        """Get element bounding box via DOM.getBoxModel. Returns (x, y, w, h)."""
        try:
            result = await self._send(
                ws,
                "DOM.getBoxModel",
                {
                    "backendNodeId": backend_node_id,
                },
            )
            model = result.get("model", {})
            border = model.get("border", [])
            if len(border) >= 8:
                # border is [x1,y1, x2,y2, x3,y3, x4,y4] — quad vertices
                x = int(border[0])
                y = int(border[1])
                w = int(border[2] - border[0])
                h = int(border[5] - border[1])
                if w > 0 and h > 0:
                    return (x, y, w, h)
        except Exception:
            pass
        return None

    async def _get_visual_summary(self, ws, backend_node_id: int) -> str:
        """Get key visual properties for an element. Returns compact description."""
        try:
            # Resolve to DOM nodeId first
            desc = await self._send(
                ws,
                "DOM.describeNode",
                {
                    "backendNodeId": backend_node_id,
                },
            )
            node_id = desc.get("node", {}).get("nodeId")
            if not node_id:
                return ""

            result = await self._send(
                ws,
                "CSS.getComputedStyleForNode",
                {
                    "nodeId": node_id,
                },
            )
            styles = {
                s["name"]: s["value"]
                for s in result.get("computedStyle", [])
                if s["name"]
                in {
                    "color",
                    "background-color",
                    "font-size",
                    "font-weight",
                    "visibility",
                    "opacity",
                    "display",
                }
            }

            if not styles:
                return ""

            # Build compact visual description
            parts = []
            bg = styles.get("background-color", "")
            if bg and bg not in ("rgba(0, 0, 0, 0)", "transparent"):
                parts.append(f"bg:{self._simplify_color(bg)}")
            fg = styles.get("color", "")
            if fg:
                parts.append(f"text:{self._simplify_color(fg)}")
            fs = styles.get("font-size", "")
            if fs:
                parts.append(fs)
            fw = styles.get("font-weight", "")
            if fw and fw not in ("400", "normal"):
                parts.append("bold" if fw in ("700", "bold") else f"fw:{fw}")
            opacity = styles.get("opacity", "1")
            if opacity != "1":
                parts.append(f"opacity:{opacity}")
            display = styles.get("display", "")
            vis = styles.get("visibility", "")
            if display == "none" or vis == "hidden":
                parts.append("HIDDEN")

            return ", ".join(parts) if parts else ""
        except Exception:
            return ""

    _COLOR_RE = re.compile(r"rgba?\((\d+),\s*(\d+),\s*(\d+)")

    @staticmethod
    def _simplify_color(css_color: str) -> str:
        """Convert rgb(r,g,b) to a readable name or short hex."""
        m = CDPClient._COLOR_RE.match(css_color)
        if not m:
            return css_color[:20]
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))

        # Map to basic color names for common cases
        _COLORS = [
            ((255, 255, 255), "white"),
            ((0, 0, 0), "black"),
            ((255, 0, 0), "red"),
            ((0, 128, 0), "green"),
            ((0, 0, 255), "blue"),
            ((128, 128, 128), "gray"),
            ((255, 255, 0), "yellow"),
            ((255, 165, 0), "orange"),
            ((128, 0, 128), "purple"),
        ]
        # Find closest by Euclidean distance
        best_name, best_dist = "", 999
        for (cr, cg, cb), name in _COLORS:
            dist = ((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_name = name
        if best_dist < 80:
            return best_name
        return f"#{r:02x}{g:02x}{b:02x}"

    async def query_elements(
        self, tab: ChromeTab, role: str = "", name: str = ""
    ) -> list[UIElement]:
        """Search for elements by role and/or accessible name."""
        self.reset_ids()
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "Accessibility.enable")

                params: dict[str, Any] = {}
                if role:
                    params["role"] = role
                if name:
                    params["accessibleName"] = name

                result = await self._send(ws, "Accessibility.queryAXTree", params)

                nodes = result.get("nodes", [])
                secure_metadata = await self.collect_secure_dom_metadata(
                    lambda method, metadata_params: self._send(
                        ws,
                        method,
                        metadata_params,
                    ),
                    nodes,
                )
                elements = []
                for node in nodes:
                    el = self._node_to_element(
                        node,
                        secure_metadata=secure_metadata,
                    )
                    if el:
                        elements.append(el)
                return elements
        except Exception:
            return []

    async def is_element_valid(self, tab: ChromeTab, backend_node_id: int) -> bool:
        """Check if a CDP element reference is still valid (not stale).

        Returns True if the element still exists in the DOM, False if stale.
        Use this before actions to give better error messages.
        """
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                result = await self._send(
                    ws,
                    "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                return object_id is not None
        except Exception:
            return False

    async def _read_document_revision(self, ws) -> int:
        frame_result = await self._send(ws, "Page.getFrameTree")
        document_result = await self._send(
            ws,
            "DOM.getDocument",
            {"depth": 0, "pierce": False},
        )
        return _document_revision(frame_result, document_result)

    async def get_document_revision(self, tab: ChromeTab) -> int:
        """Read a stable identity for the tab's current top-level document."""
        async with _connect_websocket(tab.ws_url) as ws:
            return await self._read_document_revision(ws)

    async def _assert_document_revision(self, ws, expected_revision: int) -> None:
        current_revision = await self._read_document_revision(ws)
        if current_revision != expected_revision:
            raise CDPDocumentChangedError("shadow document changed")

    async def _assert_element_semantics(
        self,
        ws,
        backend_node_id: int,
        expected_element: UIElement,
    ) -> None:
        result = await self._send(
            ws,
            "Accessibility.getPartialAXTree",
            {"backendNodeId": backend_node_id, "fetchRelatives": False},
        )
        if not ax_element_semantics_match(
            result,
            backend_node_id=backend_node_id,
            expected_role=expected_element.role,
            expected_name=expected_element.name,
        ):
            raise CDPDocumentChangedError(
                "shadow element accessibility semantics changed"
            )

    async def _element_has_exact_focus(
        self,
        ws,
        backend_node_id: int,
        expected_element: UIElement,
    ) -> bool:
        result = await self._send(
            ws,
            "Accessibility.getPartialAXTree",
            {"backendNodeId": backend_node_id, "fetchRelatives": False},
        )
        return ax_element_has_exact_focus(
            result,
            backend_node_id=backend_node_id,
            expected_role=expected_element.role,
            expected_name=expected_element.name,
        )

    async def get_element_value(
        self, tab: ChromeTab, backend_node_id: int
    ) -> str | None:
        """Get the current value of an input element.

        Returns the value string, or None if the element doesn't exist or has no value.
        Used for typing verification.
        """
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                result = await self._send(
                    ws,
                    "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return None

                # Get value via JS
                result = await self._send(
                    ws,
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": """function() {
                            if (this.value !== undefined) return this.value;
                            if (this.textContent !== undefined) return this.textContent;
                            if (this.innerText !== undefined) return this.innerText;
                            return '';
                        }""",
                        "objectId": object_id,
                        "returnByValue": True,
                    },
                )
                return result.get("result", {}).get("value", "")
        except Exception:
            return None

    async def click_element(
        self,
        tab: ChromeTab,
        backend_node_id: int,
        *,
        expected_element: UIElement,
        expected_revision: int | None = None,
    ) -> bool:
        """Click an element by its backend DOM node ID."""
        if (
            not isinstance(expected_element, UIElement)
            or expected_element.platform_ref != backend_node_id
        ):
            raise ValueError("expected_element must match backend_node_id")
        dispatched = False
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)

                # Resolve to JS object
                result = await self._send(
                    ws,
                    "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return False
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)
                await self._assert_element_semantics(
                    ws,
                    backend_node_id,
                    expected_element,
                )

                # Click via JS
                dispatched = True
                click_result = await self._send(
                    ws,
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": CLICK_FUNCTION,
                        "objectId": object_id,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                click_status = parse_runtime_action_status(
                    click_result,
                    allowed=frozenset({RuntimeActionStatus.CLICK_APPLIED}),
                )
                if click_status is not RuntimeActionStatus.CLICK_APPLIED:
                    raise AssertionError("unreachable click action status")
                return True
        except CDPDocumentChangedError:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow click outcome is unknown"
                )
            raise
        except CDPMutationOutcomeUnknown:
            raise
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow click outcome is unknown"
                ) from exc
            return False

    async def type_text(
        self,
        tab: ChromeTab,
        backend_node_id: int,
        text: str,
        *,
        expected_element: UIElement,
        expected_revision: int | None = None,
    ) -> bool:
        """Type text into an element."""
        if (
            not isinstance(expected_element, UIElement)
            or expected_element.platform_ref != backend_node_id
        ):
            raise ValueError("expected_element must match backend_node_id")
        dispatched = False
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)

                # Focus the element
                result = await self._send(
                    ws,
                    "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return False
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)
                await self._assert_element_semantics(
                    ws,
                    backend_node_id,
                    expected_element,
                )

                dispatched = True
                focus_result = await self._send(
                    ws,
                    "DOM.focus",
                    {"backendNodeId": backend_node_id},
                )
                require_empty_command_result(focus_result)
                if not await self._element_has_exact_focus(
                    ws,
                    backend_node_id,
                    expected_element,
                ):
                    dispatched = False
                    raise CDPFocusMismatchError(
                        "exact shadow element focus was not proven"
                    )
                dispatched = False
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)

                # Insert full text in one CDP call (vs 2N calls for per-char dispatch)
                dispatched = True
                await self._send(
                    ws,
                    "Input.insertText",
                    {"text": text},
                )

                return True
        except (CDPDocumentChangedError, CDPFocusMismatchError):
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow input outcome is unknown"
                )
            raise
        except CDPMutationOutcomeUnknown:
            raise
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow input outcome is unknown"
                ) from exc
            return False

    # ── New capabilities ─────────────────────────────────────────────

    async def navigate(self, tab: ChromeTab, url: str) -> dict:
        """Navigate a tab to a URL. Returns {url, title} after load."""
        dispatched = False
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                events: list[dict] = []
                await self._send(ws, "Page.enable", event_buffer=events)
                events.clear()
                dispatched = True
                result = await self._send(
                    ws,
                    "Page.navigate",
                    {"url": url},
                    event_buffer=events,
                )
                error_text = result.get("errorText")
                if error_text:
                    return {"error": error_text}

                # Events can arrive before the command response. Consume the
                # bounded buffer first, then block once on the receive stream.
                await self._wait_for_protocol_event(
                    ws,
                    {"Page.loadEventFired", "Page.domContentEventFired"},
                    deadline=asyncio.get_running_loop().time() + 12.0,
                    event_buffer=events,
                )

                # Get final title
                doc = await self._send(
                    ws,
                    "Runtime.evaluate",
                    {"expression": "document.title", "returnByValue": True},
                    event_buffer=events,
                )
                title = doc.get("result", {}).get("value", "")
                return {"url": url, "title": title}
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow navigation outcome is unknown"
                ) from exc
            return {"error": "navigation provider failure"}

    async def evaluate(self, tab: ChromeTab, expression: str) -> dict:
        """Execute JavaScript in the tab. Returns {value} or {error}.

        Return values are truncated to MAX_EVAL_RESULT_LEN to prevent
        excessive data from flooding the MCP response.
        """
        logger.debug("eyes_evaluate request (%d characters)", len(expression))

        dispatched = False
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                dispatched = True
                result = await self._send(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                exception = result.get("exceptionDetails")
                if exception:
                    text = exception.get("text", "")
                    ex = exception.get("exception", {})
                    desc = ex.get("description", "") if isinstance(ex, dict) else ""
                    return {"error": desc or text or "JS exception"}

                val = result.get("result", {}).get("value")
                # Truncate large return values
                if isinstance(val, str) and len(val) > MAX_EVAL_RESULT_LEN:
                    val = (
                        val[:MAX_EVAL_RESULT_LEN]
                        + f"… [truncated, {len(val)} chars total]"
                    )
                return {"value": val}
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow JavaScript outcome is unknown"
                ) from exc
            return {"error": "evaluation provider failure"}

    async def press_key(
        self, tab: ChromeTab, key: str, modifiers: list[str] | None = None
    ) -> bool:
        """Press a key (Enter, Tab, Escape, etc.) with optional modifiers."""
        mod_flags = 0
        for m in modifiers or []:
            m_lower = m.lower()
            if m_lower in ("alt", "option"):
                mod_flags |= 1
            elif m_lower in ("ctrl", "control"):
                mod_flags |= 2
            elif m_lower in ("meta", "cmd", "command"):
                mod_flags |= 4
            elif m_lower == "shift":
                mod_flags |= 8

        key_lower = key.lower()
        key_desc = _KEY_MAP.get(key_lower)

        if not key_desc:
            # Single printable character
            key_desc = {
                "key": key,
                "code": f"Key{key.upper()}" if len(key) == 1 else key,
                "keyCode": ord(key.upper()) if len(key) == 1 else 0,
                "text": key if len(key) == 1 else "",
            }

        try:
            async with _connect_websocket(tab.ws_url) as ws:
                down_params: dict[str, Any] = {
                    "type": "keyDown",
                    "key": key_desc["key"],
                    "code": key_desc.get("code", ""),
                    "windowsVirtualKeyCode": key_desc.get("keyCode", 0),
                    "nativeVirtualKeyCode": key_desc.get("keyCode", 0),
                    "modifiers": mod_flags,
                }
                if key_desc.get("text"):
                    down_params["text"] = key_desc["text"]

                await self._send(ws, "Input.dispatchKeyEvent", down_params)
                await self._send(
                    ws,
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": key_desc["key"],
                        "code": key_desc.get("code", ""),
                        "windowsVirtualKeyCode": key_desc.get("keyCode", 0),
                        "nativeVirtualKeyCode": key_desc.get("keyCode", 0),
                        "modifiers": mod_flags,
                    },
                )
                return True
        except Exception:
            return False

    async def wait_for_element(
        self,
        tab: ChromeTab,
        role: str = "",
        name: str = "",
        timeout: float = 5.0,
    ) -> UIElement | None:
        """Wait for an accessibility match, re-querying only after CDP events.

        This legacy one-WebSocket path remains for compatibility with callers
        that do not use :class:`CDPConnection`; it does not use timer polling.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        event_methods = {
            "Accessibility.nodesUpdated",
            "Accessibility.loadComplete",
        }
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                events: list[dict] = []
                await self._send(
                    ws,
                    "Accessibility.enable",
                    event_buffer=events,
                )

                async def query() -> UIElement | None:
                    root_result = await self._send(
                        ws,
                        "Accessibility.getRootAXNode",
                        event_buffer=events,
                    )
                    root = root_result.get("node", {})
                    params: dict[str, Any] = {}
                    backend_node_id = root.get("backendDOMNodeId")
                    node_id = root.get("nodeId")
                    if backend_node_id is not None:
                        params["backendNodeId"] = backend_node_id
                    elif node_id:
                        params["nodeId"] = node_id
                    else:
                        return None
                    if role:
                        params["role"] = role
                    if name:
                        params["accessibleName"] = name
                    result = await self._send(
                        ws,
                        "Accessibility.queryAXTree",
                        params,
                        event_buffer=events,
                    )
                    for node in result.get("nodes", []):
                        element = self._node_to_element(node)
                        if element is not None:
                            return element
                    return None

                while True:
                    match = await query()
                    if match is not None:
                        return match

                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return None
                    event = await self._wait_for_protocol_event(
                        ws,
                        event_methods,
                        deadline=deadline,
                        event_buffer=events,
                    )
                    if event is None:
                        return None
        except asyncio.TimeoutError:
            return None
        except Exception:
            pass
        return None

    async def new_tab(self, url: str = "about:blank") -> ChromeTab | None:
        """Open a new browser tab via Target.createTarget and return it.

        For non-blank URLs, opens a WebSocket to the new tab and waits for
        Page.loadEventFired or Page.domContentEventFired (up to 10s), then
        fetches the actual page title — matching the behaviour of navigate().
        """
        # Need an existing tab's WS URL to issue the browser-level command
        tabs = await self.list_tabs()
        if not tabs:
            return None

        dispatched = False
        try:
            async with _connect_websocket(tabs[0].ws_url) as ws:
                dispatched = True
                result = await self._send(ws, "Target.createTarget", {"url": url})
                target_id = result.get("targetId")
                if not target_id:
                    return None
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow new-tab outcome is unknown"
                ) from exc
            return None

        # Target.createTarget acknowledged creation. Metadata discovery may lag,
        # so retain the browser-owned ID instead of inviting a duplicate retry.
        new_tabs = await self.list_tabs()
        new_tab = next(
            (candidate for candidate in new_tabs if candidate.id == target_id), None
        )
        if new_tab is None:
            return ChromeTab(id=target_id, title="", url=url, ws_url="")

        # For non-blank URLs: observe ready state/events and get the real title.
        if url != "about:blank" and new_tab.ws_url:
            try:
                async with _connect_websocket(new_tab.ws_url) as ws:
                    events: list[dict] = []
                    await self._send(ws, "Page.enable", event_buffer=events)
                    ready = await self._send(
                        ws,
                        "Runtime.evaluate",
                        {
                            "expression": "document.readyState",
                            "returnByValue": True,
                        },
                        event_buffer=events,
                    )
                    ready_state = ready.get("result", {}).get("value", "")
                    if ready_state not in {"interactive", "complete"}:
                        await self._wait_for_protocol_event(
                            ws,
                            {"Page.loadEventFired", "Page.domContentEventFired"},
                            deadline=asyncio.get_running_loop().time() + 10.0,
                            event_buffer=events,
                        )
                    doc = await self._send(
                        ws,
                        "Runtime.evaluate",
                        {"expression": "document.title", "returnByValue": True},
                        event_buffer=events,
                    )
                    title = doc.get("result", {}).get("value", "")
                    if title:
                        new_tab = ChromeTab(
                            id=new_tab.id,
                            title=title,
                            url=url,
                            ws_url=new_tab.ws_url,
                        )
            except Exception as exc:
                logger.debug(
                    "new_tab page observation failed (exception_type=%s)",
                    type(exc).__name__,
                )
        return new_tab

    async def close_tab(self, tab: ChromeTab) -> bool:
        """Close a browser tab."""

        def close() -> bool:
            try:
                status, _body = _cdp_http_get(
                    self.host,
                    self.active_port,
                    f"/json/close/{quote(tab.id, safe='')}",
                    timeout=5,
                )
                return status == 200
            except Exception:
                return False

        return await asyncio.to_thread(close)

    async def handle_dialog(
        self, tab: ChromeTab, accept: bool = True, prompt_text: str = ""
    ) -> bool:
        """Handle a JavaScript dialog (alert, confirm, prompt)."""
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "Page.enable")
                params: dict[str, Any] = {"accept": accept}
                if prompt_text:
                    params["promptText"] = prompt_text
                await self._send(ws, "Page.handleJavaScriptDialog", params)
                return True
        except Exception as e:
            logger.debug(
                "handle_dialog failed (no dialog open; exception_type=%s)",
                type(e).__name__,
            )
            return False

    async def set_file_input(
        self,
        tab: ChromeTab,
        backend_node_id: int,
        files: list[str],
        *,
        expected_revision: int | None = None,
    ) -> bool:
        """Set files on a file input element."""
        dispatched = False
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                if expected_revision is not None:
                    await self._assert_document_revision(ws, expected_revision)
                    resolved = await self._send(
                        ws,
                        "DOM.resolveNode",
                        {"backendNodeId": backend_node_id},
                    )
                    if not resolved.get("object", {}).get("objectId"):
                        return False
                    await self._assert_document_revision(ws, expected_revision)
                dispatched = True
                await self._send(
                    ws,
                    "DOM.setFileInputFiles",
                    {"files": files, "backendNodeId": backend_node_id},
                )
                return True
        except CDPDocumentChangedError:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow file-input outcome is unknown"
                )
            raise
        except CDPMutationOutcomeUnknown:
            raise
        except Exception as exc:
            if dispatched:
                raise CDPMutationOutcomeUnknown(
                    "legacy shadow file-input outcome is unknown"
                ) from exc
            return False

    async def scroll(
        self,
        tab: ChromeTab,
        x: int = 0,
        y: int = 0,
        delta_x: int = 0,
        delta_y: int = 0,
    ) -> bool:
        """Scroll the page at coordinates (x, y) by (delta_x, delta_y)."""
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(
                    ws,
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": delta_x,
                        "deltaY": delta_y,
                    },
                )
                return True
        except Exception:
            return False

    async def drag(
        self,
        tab: ChromeTab,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        steps: int = 10,
    ) -> bool:
        """Drag from one point to another with smooth mouse movement."""
        try:
            async with _connect_websocket(tab.ws_url) as ws:
                # Mouse down at start
                await self._send(
                    ws,
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": from_x,
                        "y": from_y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )

                # Smooth move
                for i in range(1, steps + 1):
                    frac = i / steps
                    cx = int(from_x + (to_x - from_x) * frac)
                    cy = int(from_y + (to_y - from_y) * frac)
                    await self._send(
                        ws,
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseMoved",
                            "x": cx,
                            "y": cy,
                            "button": "left",
                        },
                    )

                # Mouse up at end
                await self._send(
                    ws,
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": to_x,
                        "y": to_y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
                return True
        except Exception:
            return False

    async def get_page_content_summary(self, tab: ChromeTab) -> str:
        """Get a structured accessibility summary of a web page via CDP.

        Cross-platform: works on macOS, Linux, and Windows — anywhere CDP is available.
        This is the equivalent of applescript.get_page_accessibility_summary() but
        uses CDP JavaScript execution instead of AppleScript/JXA.
        """
        js_code = """
        (function() {
            var result = [];

            // Headings
            var headings = document.querySelectorAll('h1,h2,h3,h4,h5,h6');
            if (headings.length) {
                result.push('HEADINGS:');
                headings.forEach(function(h) {
                    result.push('  ' + h.tagName + ': ' + h.textContent.trim().substring(0, 150));
                });
            }

            // Navigation
            var navs = document.querySelectorAll('nav, [role="navigation"]');
            if (navs.length) {
                result.push('NAVIGATION:');
                navs.forEach(function(n, i) {
                    var label = n.getAttribute('aria-label') || ('nav-' + i);
                    var links = n.querySelectorAll('a');
                    result.push('  ' + label + ' (' + links.length + ' links)');
                });
            }

            // Buttons
            var buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
            if (buttons.length) {
                result.push('BUTTONS (' + buttons.length + '):');
                var shown = Math.min(buttons.length, 20);
                for (var i = 0; i < shown; i++) {
                    var b = buttons[i];
                    var text = b.textContent.trim() || b.getAttribute('aria-label') || b.getAttribute('title') || '';
                    result.push('  - ' + text.substring(0, 100));
                }
                if (buttons.length > 20) result.push('  ... and ' + (buttons.length - 20) + ' more');
            }

            // Inputs
            var inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
            if (inputs.length) {
                result.push('INPUTS (' + inputs.length + '):');
                inputs.forEach(function(inp) {
                    var label = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || inp.name || inp.type;
                    var val = inp.value ? ' = "' + inp.value.substring(0, 50) + '"' : '';
                    result.push('  - ' + inp.tagName.toLowerCase() + '[' + (inp.type || '') + '] ' + label + val);
                });
            }

            // Links
            var links = document.querySelectorAll('a[href]');
            if (links.length) {
                result.push('LINKS (' + links.length + '):');
                var shown = Math.min(links.length, 15);
                for (var i = 0; i < shown; i++) {
                    var a = links[i];
                    var text = a.textContent.trim() || a.getAttribute('aria-label') || '';
                    result.push('  - ' + text.substring(0, 80));
                }
                if (links.length > 15) result.push('  ... and ' + (links.length - 15) + ' more');
            }

            // Chat/list items (WhatsApp, Slack, etc.)
            var chatItems = document.querySelectorAll('[role="listitem"], [role="row"], [data-testid*="chat"], [class*="chat-list"] > div');
            if (chatItems.length) {
                result.push('CHAT/LIST ITEMS (' + chatItems.length + '):');
                var shown = Math.min(chatItems.length, 20);
                for (var i = 0; i < shown; i++) {
                    var item = chatItems[i];
                    var text = item.textContent.trim().replace(/\\s+/g, ' ').substring(0, 150);
                    result.push('  [' + i + '] ' + text);
                }
                if (chatItems.length > 20) result.push('  ... and ' + (chatItems.length - 20) + ' more');
            }

            return result.join('\\n');
        })()
        """

        try:
            async with _connect_websocket(tab.ws_url) as ws:
                result = await self._send(
                    ws,
                    "Runtime.evaluate",
                    {"expression": js_code, "returnByValue": True},
                )
                value = result.get("result", {}).get("value", "")
                return value if value else ""
        except Exception as e:
            return f"ERROR: CDP JS execution failed: {e}"

    @staticmethod
    def _pop_buffered_event(
        event_buffer: list[dict],
        methods: set[str],
    ) -> dict | None:
        for index, message in enumerate(event_buffer):
            if message.get("method") in methods:
                return event_buffer.pop(index)
        return None

    async def _wait_for_protocol_event(
        self,
        ws,
        methods: set[str],
        *,
        deadline: float,
        event_buffer: list[dict],
    ) -> dict | None:
        buffered = self._pop_buffered_event(event_buffer, methods)
        if buffered is not None:
            return buffered

        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            message = json.loads(raw)
            if message.get("method") in methods:
                return message
            if isinstance(message.get("method"), str):
                event_buffer.append(message)
                if len(event_buffer) > _MAX_BUFFERED_EVENTS:
                    del event_buffer[: len(event_buffer) - _MAX_BUFFERED_EVENTS]

    async def _send(
        self,
        ws,
        method: str,
        params: dict | None = None,
        timeout: float = 15.0,
        *,
        event_buffer: list[dict] | None = None,
    ) -> dict:
        """Send a CDP command and wait for response.

        Uses a locally-captured message ID to avoid race conditions
        if multiple coroutines ever share a CDPClient instance.
        """
        self._msg_id += 1
        msg_id = self._msg_id  # capture locally before any await
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        await ws.send(json.dumps(msg))

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(f"CDP command {method} timed out after {timeout}s")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise RuntimeError(f"CDP command {method} timed out after {timeout}s")
            response = json.loads(raw)
            if response.get("id") == msg_id:
                if "error" in response:
                    raise RuntimeError(f"CDP command {method} failed")
                return response.get("result", {})
            if event_buffer is not None and isinstance(response.get("method"), str):
                event_buffer.append(response)
                if len(event_buffer) > _MAX_BUFFERED_EVENTS:
                    del event_buffer[: len(event_buffer) - _MAX_BUFFERED_EVENTS]

    def _build_tree(
        self,
        nodes: list[dict],
        *,
        secure_metadata: SecureDOMMetadata | None = None,
    ) -> UIElement | None:
        """Build UIElement tree from flat CDP node list."""
        if not nodes:
            return None
        nodes, nodes_truncated = _bounded_ax_nodes(nodes)
        if not nodes:
            if nodes_truncated:
                return UIElement(
                    id=self._next_id(),
                    role="document",
                    name="Accessibility tree conversion limit reached",
                    states=["truncated"],
                    source="cdp",
                )
            return None

        # Build lookup
        node_map: dict[str, dict] = {}
        for node in nodes:
            node_map[node["nodeId"]] = node

        # Find root (first non-ignored node)
        root_node = None
        for node in nodes:
            if not node.get("ignored", False):
                root_node = node
                break

        if root_node is None and nodes:
            root_node = nodes[0]

        if root_node is None:
            return None

        roots = self._build_subtree(
            root_node,
            node_map,
            set(),
            secure_metadata=secure_metadata,
        )
        if len(roots) == 1:
            tree = roots[0]
        elif roots:
            tree = UIElement(id=self._next_id(), role="group", source="cdp")
            tree.children = roots
        else:
            tree = None
        if tree is not None and nodes_truncated:
            tree.states.append("truncated")
        return tree

    def _build_subtree(
        self,
        node: dict,
        node_map: dict,
        visited: set,
        *,
        secure_metadata: SecureDOMMetadata | None,
    ) -> list[UIElement]:
        node_id = node.get("nodeId", "")
        if node_id in visited:
            return []
        visited.add(node_id)

        role_value = node.get("role", {})
        role = (
            role_value.get("value", "unknown")
            if isinstance(role_value, dict)
            else str(role_value)
        )
        if role == "InlineTextBox":
            return []

        transparent = node.get("ignored", False) or role in {"none", "generic"}
        if transparent:
            descendants: list[UIElement] = []
            for child_id in node.get("childIds", []):
                if child_id in node_map:
                    descendants.extend(
                        self._build_subtree(
                            node_map[child_id],
                            node_map,
                            visited,
                            secure_metadata=secure_metadata,
                        )
                    )
            return descendants

        element = self._node_to_element(
            node,
            secure_metadata=secure_metadata,
        )
        if element is None:
            return []

        for child_id in node.get("childIds", []):
            if child_id in node_map:
                element.children.extend(
                    self._build_subtree(
                        node_map[child_id],
                        node_map,
                        visited,
                        secure_metadata=secure_metadata,
                    )
                )

        return [element]

    def _node_to_element(
        self,
        node: dict,
        *,
        secure_metadata: SecureDOMMetadata | None = None,
    ) -> UIElement | None:
        """Convert a CDP AXNode to UIElement."""
        role_val = node.get("role", {})
        role = (
            role_val.get("value", "unknown")
            if isinstance(role_val, dict)
            else str(role_val)
        )

        if role in ("none", "generic", "InlineTextBox"):
            return None

        name_val = node.get("name", {})
        name = (
            name_val.get("value", "")
            if isinstance(name_val, dict)
            else str(name_val or "")
        )

        value_val = node.get("value", {})
        value = (
            value_val.get("value", "")
            if isinstance(value_val, dict)
            else str(value_val or "")
        )

        desc_val = node.get("description", {})
        description = (
            desc_val.get("value", "")
            if isinstance(desc_val, dict)
            else str(desc_val or "")
        )

        # Extract states from properties. Chromium normally identifies native
        # password controls here, but DOM metadata remains authoritative for
        # custom/autocomplete/CSS-secured textboxes.
        states = []
        ax_secure = False
        for prop in node.get("properties", []):
            prop_name = prop.get("name", "")
            prop_value = prop.get("value", {}).get("value", False)
            if prop_name == "focused" and prop_value:
                states.append("focused")
            elif prop_name == "disabled" and prop_value:
                states.append("disabled")
            elif prop_name == "selected" and prop_value:
                states.append("selected")
            elif prop_name == "required" and prop_value:
                states.append("required")
            elif prop_name == "checked":
                if prop_value == "true" or prop_value is True:
                    states.append("checked")
            elif prop_name in {"password", "protected", "secure"}:
                if prop_value is True or str(prop_value).casefold() == "true":
                    ax_secure = True

        # Store backend DOM node ID for actions
        backend_id = node.get("backendDOMNodeId")
        secure_backend_ids = (
            secure_metadata.secure_backend_node_ids
            if secure_metadata is not None
            else frozenset()
        )
        secure = _tree_node_is_secure({"role": role, "secure": ax_secure}) or (
            isinstance(backend_id, int)
            and not isinstance(backend_id, bool)
            and backend_id in secure_backend_ids
        )
        metadata_complete = secure_metadata is not None and secure_metadata.complete
        redact_unverified = (
            str(role).casefold() in _TEXT_VALUE_ROLES
            and bool(value)
            and not metadata_complete
        )
        if secure:
            states.append("secure")
        elif redact_unverified:
            states.append("value-redacted")

        return UIElement(
            id=self._next_id(),
            role=role,
            name=name,
            value=""
            if secure or redact_unverified
            else str(value)[:200]
            if value
            else "",
            description=description,
            states=states,
            platform_ref=backend_id,  # Store backend DOM node ID
            source="cdp",
        )

    async def get_pierced_dom(
        self,
        tab: ChromeTab,
        *,
        max_nodes: int = _MAX_PIERCED_DOM_NODES,
    ) -> list[dict]:
        """Read a bounded set of named controls across open shadow roots.

        A whole-document ``depth=-1`` response can allocate thousands of nodes
        before a local cap can help. ``DOM.performSearch`` applies the cap in
        Chrome first, then only the exact matches needed by shadow augmentation
        are described.
        """
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or not 1 <= max_nodes <= _MAX_PIERCED_DOM_NODES
        ):
            raise ValueError(
                f"max_nodes must be an integer between 1 and {_MAX_PIERCED_DOM_NODES}"
            )

        try:
            async with _connect_websocket(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                search_id = ""
                try:
                    search = await self._send(
                        ws,
                        "DOM.performSearch",
                        {
                            "query": _PIERCED_CONTROL_SELECTOR,
                            "includeUserAgentShadowDOM": True,
                        },
                        timeout=3.0,
                    )
                    search_id = search.get("searchId", "")
                    result_count = search.get("resultCount", 0)
                    if not isinstance(search_id, str) or not search_id:
                        return []
                    if (
                        isinstance(result_count, bool)
                        or not isinstance(result_count, int)
                        or result_count <= 0
                    ):
                        return []
                    to_index = min(result_count, max_nodes)
                    results = await self._send(
                        ws,
                        "DOM.getSearchResults",
                        {
                            "searchId": search_id,
                            "fromIndex": 0,
                            "toIndex": to_index,
                        },
                        timeout=3.0,
                    )
                    node_ids = results.get("nodeIds", [])
                    if not isinstance(node_ids, list):
                        return []

                    nodes: list[dict] = []
                    for node_id in node_ids[:to_index]:
                        if (
                            isinstance(node_id, bool)
                            or not isinstance(node_id, int)
                            or node_id <= 0
                        ):
                            continue
                        description = await self._send(
                            ws,
                            "DOM.describeNode",
                            {"nodeId": node_id, "depth": 0},
                            timeout=3.0,
                        )
                        node = description.get("node")
                        if isinstance(node, dict):
                            nodes.append(node)
                    return nodes
                finally:
                    if search_id:
                        try:
                            await self._send(
                                ws,
                                "DOM.discardSearchResults",
                                {"searchId": search_id},
                                timeout=1.0,
                            )
                        except Exception as exc:
                            logger.debug(
                                "Could not discard legacy DOM search state "
                                "(exception_type=%s)",
                                type(exc).__name__,
                            )
        except Exception as exc:
            logger.debug(
                "get_pierced_dom failed with %s (non-critical)",
                type(exc).__name__,
            )
            return []
