"""Chrome DevTools Protocol client for enriched accessibility trees.

Cross-platform: works on macOS, Windows, and Linux — anywhere Chrome runs.
Gets accessibility trees enriched with bounding boxes and visual metadata
(colors, fonts, layout) via DOM/CSS domains — giving AI agents spatial
awareness and visual context without screenshots.
"""
from __future__ import annotations

import json
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .adapters.base import UIElement
from .platform_utils import discover_cdp_port

logger = logging.getLogger("agent-eyes")

# Key name → CDP key descriptor mapping for special keys
_KEY_MAP: dict[str, dict[str, Any]] = {
    "enter":     {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "return":    {"key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r"},
    "tab":       {"key": "Tab", "code": "Tab", "keyCode": 9},
    "escape":    {"key": "Escape", "code": "Escape", "keyCode": 27},
    "esc":       {"key": "Escape", "code": "Escape", "keyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "keyCode": 8},
    "delete":    {"key": "Delete", "code": "Delete", "keyCode": 46},
    "space":     {"key": " ", "code": "Space", "keyCode": 32, "text": " "},
    "arrowup":   {"key": "ArrowUp", "code": "ArrowUp", "keyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "keyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37},
    "arrowright":{"key": "ArrowRight", "code": "ArrowRight", "keyCode": 39},
    "home":      {"key": "Home", "code": "Home", "keyCode": 36},
    "end":       {"key": "End", "code": "End", "keyCode": 35},
    "pageup":    {"key": "PageUp", "code": "PageUp", "keyCode": 33},
    "pagedown":  {"key": "PageDown", "code": "PageDown", "keyCode": 34},
    "f1":        {"key": "F1", "code": "F1", "keyCode": 112},
    "f2":        {"key": "F2", "code": "F2", "keyCode": 113},
    "f3":        {"key": "F3", "code": "F3", "keyCode": 114},
    "f4":        {"key": "F4", "code": "F4", "keyCode": 115},
    "f5":        {"key": "F5", "code": "F5", "keyCode": 116},
    "f6":        {"key": "F6", "code": "F6", "keyCode": 117},
    "f7":        {"key": "F7", "code": "F7", "keyCode": 118},
    "f8":        {"key": "F8", "code": "F8", "keyCode": 119},
    "f9":        {"key": "F9", "code": "F9", "keyCode": 120},
    "f10":       {"key": "F10", "code": "F10", "keyCode": 121},
    "f11":       {"key": "F11", "code": "F11", "keyCode": 122},
    "f12":       {"key": "F12", "code": "F12", "keyCode": 123},
}


@dataclass
class ChromeTab:
    """A Chrome browser tab."""
    id: str
    title: str
    url: str
    ws_url: str


class CDPClient:
    """Lightweight Chrome DevTools Protocol client using websockets.

    Auto-discovers Chrome's CDP port from DevToolsActivePort file,
    falling back to the specified port (default 9222).
    """

    def __init__(self, host: str = "localhost", port: int = 9222):
        self.host = host
        self.port = port
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
                    f"http://{self.host}:{port}/json/version",
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
            import urllib.request
            try:
                req = urllib.request.Request(
                    f"http://{self.host}:{port}/json/version"
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    return resp.status == 200
            except Exception:
                return False
        return await asyncio.get_running_loop().run_in_executor(None, _check_sync)

    async def list_tabs(self) -> list[ChromeTab]:
        """List all Chrome tabs."""
        def _list_sync() -> list[ChromeTab]:
            import urllib.request
            try:
                req = urllib.request.Request(
                    f"http://{self.host}:{self.active_port}/json"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())

                tabs = []
                for item in data:
                    if item.get("type") != "page":
                        continue
                    ws_url = item.get("webSocketDebuggerUrl", "")
                    if not ws_url:
                        continue
                    # Validate WebSocket URL points to localhost
                    import urllib.parse
                    parsed_ws = urllib.parse.urlparse(ws_url)
                    if parsed_ws.hostname not in ("localhost", "127.0.0.1", "::1"):
                        logger.warning(
                            "CDPClient: suspicious WS host %s — skipping tab",
                            parsed_ws.hostname,
                        )
                        continue
                    tabs.append(ChromeTab(
                        id=item.get("id", ""),
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        ws_url=ws_url,
                    ))
                return tabs
            except Exception:
                return []
        return await asyncio.get_running_loop().run_in_executor(None, _list_sync)

    async def get_accessibility_tree(
        self, tab: ChromeTab, max_depth: int = 5, enrich: bool = True
    ) -> UIElement | None:
        """Get the accessibility tree of a Chrome tab as a UIElement tree.

        When enrich=True, interactive elements get bounding boxes and
        visual metadata (colors, font) via DOM/CSS domains — giving
        Claude spatial awareness and visual context without screenshots.
        """
        import websockets

        self.reset_ids()
        try:
            async with websockets.connect(tab.ws_url) as ws:
                # Enable accessibility domain
                await self._send(ws, "Accessibility.enable")

                # Get full AX tree
                result = await self._send(
                    ws,
                    "Accessibility.getFullAXTree",
                    {"depth": max_depth},
                )

                nodes = result.get("nodes", [])
                if not nodes:
                    return None

                tree = self._build_tree(nodes)

                # Enrich interactive elements with layout + visual data
                if enrich and tree:
                    await self._enrich_tree(ws, tree)

                return tree
        except Exception as e:
            return UIElement(
                id=self._next_id(),
                role="error",
                name=f"CDP connection failed: {e}",
            )

    # Interactive roles worth enriching with layout/visual data
    _ENRICH_ROLES = frozenset({
        "button", "link", "textbox", "combobox", "searchbox",
        "checkbox", "radio", "switch", "slider", "spinbutton",
        "menuitem", "tab", "heading", "img", "banner", "navigation",
        "main", "complementary", "contentinfo", "form",
    })

    async def _enrich_tree(self, ws, element: UIElement, limit: int = 60) -> int:
        """Walk tree and enrich interactive elements with bounds + visual info.

        Enriches up to `limit` elements to keep CDP calls bounded.
        Returns number of elements enriched.

        CSS.enable and DOM.enable are called once here before the walk,
        not per-element inside _get_visual_summary / _get_box_model.
        """
        # Enable domains once before walking the tree
        await self._send(ws, "DOM.enable")
        await self._send(ws, "CSS.enable")
        return await self._enrich_subtree(ws, element, limit)

    async def _enrich_subtree(self, ws, element: UIElement, limit: int) -> int:
        """Recursive helper for _enrich_tree (domains already enabled)."""
        if limit <= 0:
            return 0
        enriched = 0

        if element.role in self._ENRICH_ROLES and element.platform_ref:
            try:
                # Run box model and visual summary in parallel (halves enrichment time)
                box, vis = await asyncio.gather(
                    self._get_box_model(ws, element.platform_ref),
                    self._get_visual_summary(ws, element.platform_ref),
                )
                if box:
                    element.bounds = box
                if vis:
                    element.visual = vis

                enriched += 1
            except Exception:
                pass  # Non-critical — tree still works without enrichment

        for child in element.children:
            if enriched >= limit:
                break
            enriched += await self._enrich_subtree(ws, child, limit - enriched)

        return enriched

    async def _get_box_model(self, ws, backend_node_id: int) -> tuple[int, int, int, int] | None:
        """Get element bounding box via DOM.getBoxModel. Returns (x, y, w, h)."""
        try:
            result = await self._send(ws, "DOM.getBoxModel", {
                "backendNodeId": backend_node_id,
            })
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
            desc = await self._send(ws, "DOM.describeNode", {
                "backendNodeId": backend_node_id,
            })
            node_id = desc.get("node", {}).get("nodeId")
            if not node_id:
                return ""

            result = await self._send(ws, "CSS.getComputedStyleForNode", {
                "nodeId": node_id,
            })
            styles = {
                s["name"]: s["value"]
                for s in result.get("computedStyle", [])
                if s["name"] in {
                    "color", "background-color", "font-size", "font-weight",
                    "visibility", "opacity", "display",
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
                parts.append(f"bold" if fw in ("700", "bold") else f"fw:{fw}")
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
            ((255, 255, 255), "white"), ((0, 0, 0), "black"),
            ((255, 0, 0), "red"), ((0, 128, 0), "green"), ((0, 0, 255), "blue"),
            ((128, 128, 128), "gray"), ((255, 255, 0), "yellow"),
            ((255, 165, 0), "orange"), ((128, 0, 128), "purple"),
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
        import websockets

        self.reset_ids()
        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "Accessibility.enable")

                params: dict[str, Any] = {}
                if role:
                    params["role"] = role
                if name:
                    params["accessibleName"] = name

                result = await self._send(
                    ws, "Accessibility.queryAXTree", params
                )

                elements = []
                for node in result.get("nodes", []):
                    el = self._node_to_element(node)
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
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                result = await self._send(
                    ws, "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                return object_id is not None
        except Exception:
            return False

    async def get_element_value(self, tab: ChromeTab, backend_node_id: int) -> str | None:
        """Get the current value of an input element.

        Returns the value string, or None if the element doesn't exist or has no value.
        Used for typing verification.
        """
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                result = await self._send(
                    ws, "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return None

                # Get value via JS
                result = await self._send(
                    ws, "Runtime.callFunctionOn",
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

    async def click_element(self, tab: ChromeTab, backend_node_id: int) -> bool:
        """Click an element by its backend DOM node ID."""
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")

                # Resolve to JS object
                result = await self._send(
                    ws, "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return False

                # Click via JS
                await self._send(
                    ws, "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": "function() { this.click(); }",
                        "objectId": object_id,
                    },
                )
                return True
        except Exception:
            return False

    async def type_text(self, tab: ChromeTab, backend_node_id: int, text: str) -> bool:
        """Type text into an element."""
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")

                # Focus the element
                result = await self._send(
                    ws, "DOM.resolveNode",
                    {"backendNodeId": backend_node_id},
                )
                object_id = result.get("object", {}).get("objectId")
                if not object_id:
                    return False

                await self._send(
                    ws, "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": "function() { this.focus(); }",
                        "objectId": object_id,
                    },
                )

                # Insert full text in one CDP call (vs 2N calls for per-char dispatch)
                await self._send(
                    ws, "Input.insertText",
                    {"text": text},
                )

                return True
        except Exception:
            return False

    # ── New capabilities ─────────────────────────────────────────────

    async def navigate(self, tab: ChromeTab, url: str) -> dict:
        """Navigate a tab to a URL. Returns {url, title} after load."""
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "Page.enable")
                result = await self._send(
                    ws, "Page.navigate", {"url": url}
                )
                error_text = result.get("errorText")
                if error_text:
                    return {"error": error_text}

                # Wait for load event (up to 10s)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        msg = json.loads(raw)
                        if msg.get("method") in (
                            "Page.loadEventFired",
                            "Page.domContentEventFired",
                        ):
                            break
                    except asyncio.TimeoutError:
                        continue

                # Get final title
                doc = await self._send(
                    ws, "Runtime.evaluate",
                    {"expression": "document.title", "returnByValue": True},
                )
                title = doc.get("result", {}).get("value", "")
                return {"url": url, "title": title}
        except Exception as e:
            return {"error": str(e)}

    async def evaluate(self, tab: ChromeTab, expression: str) -> dict:
        """Execute JavaScript in the tab. Returns {value} or {error}.

        Return values are truncated to MAX_EVAL_RESULT_LEN to prevent
        excessive data from flooding the MCP response.
        """
        import websockets

        MAX_EVAL_RESULT_LEN = 10_000

        logger.debug(
            "eyes_evaluate (len=%d): %.200s", len(expression), expression
        )

        try:
            async with websockets.connect(tab.ws_url) as ws:
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
                    val = val[:MAX_EVAL_RESULT_LEN] + f"… [truncated, {len(val)} chars total]"
                return {"value": val}
        except Exception as e:
            return {"error": str(e)}

    async def press_key(
        self, tab: ChromeTab, key: str, modifiers: list[str] | None = None
    ) -> bool:
        """Press a key (Enter, Tab, Escape, etc.) with optional modifiers."""
        import websockets

        mod_flags = 0
        for m in (modifiers or []):
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
            async with websockets.connect(tab.ws_url) as ws:
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
        """Poll for an element to appear in the accessibility tree.

        Holds a single WebSocket connection open across poll iterations
        to avoid repeated handshake overhead.
        """
        import websockets

        deadline = time.monotonic() + timeout
        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "Accessibility.enable")
                while time.monotonic() < deadline:
                    params: dict[str, Any] = {}
                    if role:
                        params["role"] = role
                    if name:
                        params["accessibleName"] = name
                    result = await self._send(
                        ws, "Accessibility.queryAXTree", params
                    )
                    nodes = result.get("nodes", [])
                    for node in nodes:
                        el = self._node_to_element(node)
                        if el:
                            return el
                    await asyncio.sleep(0.5)
        except Exception:
            pass
        return None

    async def new_tab(self, url: str = "about:blank") -> ChromeTab | None:
        """Open a new browser tab via Target.createTarget and return it.

        For non-blank URLs, opens a WebSocket to the new tab and waits for
        Page.loadEventFired or Page.domContentEventFired (up to 10s), then
        fetches the actual page title — matching the behaviour of navigate().
        """
        import websockets

        # Need an existing tab's WS URL to issue the browser-level command
        tabs = await self.list_tabs()
        if not tabs:
            return None

        try:
            async with websockets.connect(tabs[0].ws_url) as ws:
                result = await self._send(
                    ws, "Target.createTarget", {"url": url}
                )
                target_id = result.get("targetId")
                if not target_id:
                    return None

            # Fetch the new tab's metadata from the JSON endpoint
            new_tabs = await self.list_tabs()
            new_tab: ChromeTab | None = None
            for t in new_tabs:
                if t.id == target_id:
                    new_tab = t
                    break

            if new_tab is None:
                return None

            # For non-blank URLs: wait for the page to load and get the real title
            if url != "about:blank" and new_tab.ws_url:
                try:
                    async with websockets.connect(new_tab.ws_url) as ws:
                        await self._send(ws, "Page.enable")

                        # Wait for load event (up to 10s)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                                msg = json.loads(raw)
                                if msg.get("method") in (
                                    "Page.loadEventFired",
                                    "Page.domContentEventFired",
                                ):
                                    break
                            except asyncio.TimeoutError:
                                continue

                        # Get the real page title after load
                        doc = await self._send(
                            ws, "Runtime.evaluate",
                            {"expression": "document.title", "returnByValue": True},
                        )
                        title = doc.get("result", {}).get("value", "")
                        if title:
                            new_tab = ChromeTab(
                                id=new_tab.id,
                                title=title,
                                url=url,
                                ws_url=new_tab.ws_url,
                            )
                except Exception as e:
                    logger.debug("new_tab page load wait failed: %s", e)
                    # Return tab without title rather than failing entirely

            return new_tab
        except Exception as e:
            logger.error("Failed to create new tab: %s", e)
            return None

    async def close_tab(self, tab: ChromeTab) -> bool:
        """Close a browser tab."""
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://{self.host}:{self.active_port}/json/close/{tab.id}"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def handle_dialog(
        self, tab: ChromeTab, accept: bool = True, prompt_text: str = ""
    ) -> bool:
        """Handle a JavaScript dialog (alert, confirm, prompt)."""
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "Page.enable")
                params: dict[str, Any] = {"accept": accept}
                if prompt_text:
                    params["promptText"] = prompt_text
                await self._send(
                    ws, "Page.handleJavaScriptDialog", params
                )
                return True
        except Exception as e:
            logger.debug("handle_dialog failed (no dialog open?): %s", e)
            return False

    async def set_file_input(
        self, tab: ChromeTab, backend_node_id: int, files: list[str]
    ) -> bool:
        """Set files on a file input element."""
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "DOM.enable")
                await self._send(
                    ws,
                    "DOM.setFileInputFiles",
                    {"files": files, "backendNodeId": backend_node_id},
                )
                return True
        except Exception:
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
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
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
        import websockets

        try:
            async with websockets.connect(tab.ws_url) as ws:
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
                    await asyncio.sleep(0.02)

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
        import websockets

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
            async with websockets.connect(tab.ws_url) as ws:
                result = await self._send(
                    ws, "Runtime.evaluate",
                    {"expression": js_code, "returnByValue": True},
                )
                value = result.get("result", {}).get("value", "")
                return value if value else ""
        except Exception as e:
            return f"ERROR: CDP JS execution failed: {e}"

    async def _send(self, ws, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
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
                    raise RuntimeError(
                        f"CDP error: {response['error'].get('message', response['error'])}"
                    )
                return response.get("result", {})

    def _build_tree(self, nodes: list[dict]) -> UIElement | None:
        """Build UIElement tree from flat CDP node list."""
        if not nodes:
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

        return self._build_subtree(root_node, node_map, set())

    def _build_subtree(
        self, node: dict, node_map: dict, visited: set
    ) -> UIElement | None:
        node_id = node.get("nodeId", "")
        if node_id in visited:
            return None
        visited.add(node_id)

        if node.get("ignored", False):
            # Process children of ignored nodes directly
            children = []
            for child_id in node.get("childIds", []):
                if child_id in node_map:
                    child = self._build_subtree(node_map[child_id], node_map, visited)
                    if child:
                        children.append(child)
            if len(children) == 1:
                return children[0]
            if children:
                container = UIElement(id=self._next_id(), role="group")
                container.children = children
                return container
            return None

        element = self._node_to_element(node)
        if element is None:
            return None

        for child_id in node.get("childIds", []):
            if child_id in node_map:
                child = self._build_subtree(node_map[child_id], node_map, visited)
                if child:
                    element.children.append(child)

        return element

    def _node_to_element(self, node: dict) -> UIElement | None:
        """Convert a CDP AXNode to UIElement."""
        role_val = node.get("role", {})
        role = role_val.get("value", "unknown") if isinstance(role_val, dict) else str(role_val)

        if role in ("none", "generic", "InlineTextBox"):
            return None

        name_val = node.get("name", {})
        name = name_val.get("value", "") if isinstance(name_val, dict) else str(name_val or "")

        value_val = node.get("value", {})
        value = value_val.get("value", "") if isinstance(value_val, dict) else str(value_val or "")

        desc_val = node.get("description", {})
        description = desc_val.get("value", "") if isinstance(desc_val, dict) else str(desc_val or "")

        # Extract states from properties
        states = []
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

        # Store backend DOM node ID for actions
        backend_id = node.get("backendDOMNodeId")

        return UIElement(
            id=self._next_id(),
            role=role,
            name=name,
            value=str(value)[:200] if value else "",
            description=description,
            states=states,
            platform_ref=backend_id,  # Store backend DOM node ID
            source="cdp",
        )
