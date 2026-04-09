# agent-eyes Performance Redesign: Hybrid 3-Tier Architecture

**Date**: 2026-03-25
**Status**: Design approved, pending implementation plan
**Scope**: Full architecture overhaul — server (cdp.py, server.py), Chrome extension (new), skill (SKILL.md)

---

## Problem Statement

agent-eyes is too slow for web interactions on already-open browsers. Two root causes:

1. **CDP requires `--remote-debugging-port`** — can't connect to user's already-open Chrome
2. **Every CDP operation opens a new WebSocket** — ~150ms overhead per click/type/read on already-open tabs
3. **7 tools hard-fail without CDP** — no fallbacks for get_web_tree, close_tab, fill_form, drag, wait_for, handle_dialog, file_upload
4. **SKILL.md forces 4 tool calls** for every interaction (orient → read → act → verify)

## Design: 3-Tier Fallback Architecture

```
Tool call arrives
    │
    ▼
┌─────────────────────────────────────────────┐
│ Tier 1: Chrome Extension Bridge             │
│ (if installed)                              │
│ • chrome.scripting for DOM/JS               │
│ • chrome.tabs for tab management            │
│ • chrome.debugger for full CDP (optional)   │
│ • Native Messaging for IPC (1-5ms)          │
│ • Cross-platform, no flags needed           │
├─────────────────────────────────────────────┤
│ Tier 2: CDP Direct (Persistent WebSocket)   │
│ (if --remote-debugging-port or              │
│  chrome://inspect toggle enabled)           │
│ • Single WebSocket + flat sessions          │
│ • Target.setAutoAttach(flatten: true)       │
│ • Domains enabled once, persisted           │
│ • 10-50x faster than current CDP            │
├─────────────────────────────────────────────┤
│ Tier 3: Native AX + OS Fallback            │
│ (always available, zero setup)              │
│ • AX tree for reading (all platforms)       │
│ • AppleScript JS injection (macOS)          │
│ • OS input simulation (all platforms)       │
│ • JS-based pseudo-AX-tree as fallback       │
└─────────────────────────────────────────────┘
    │
    ▼
Desktop/Native apps → Always use Native AX (unchanged)
```

### Tier Selection Logic

```python
class ConnectionTier(Enum):
    EXTENSION = 1   # Chrome Extension bridge
    CDP = 2         # Direct CDP persistent WebSocket
    NATIVE = 3      # Native AX + AppleScript + OS input

async def get_best_tier() -> ConnectionTier:
    if extension_bridge.is_connected():
        return ConnectionTier.EXTENSION
    if cdp_pool.is_connected():
        return ConnectionTier.CDP
    return ConnectionTier.NATIVE
```

Each tool handler calls `get_best_tier()` and dispatches to the appropriate implementation. Tier detection is cached and re-evaluated only on connection state changes.

---

## Tier 1: Chrome Extension Bridge

### Architecture

```
agent-eyes MCP Server (Python)
    │
    │ Native Messaging (stdin/stdout)
    │ 4-byte LE length + JSON
    │ Persistent bidirectional stream
    │ 1-5ms latency per message
    │
    ▼
Chrome Extension (Service Worker)
    │
    ├── chrome.tabs API (tab management, <1ms)
    ├── chrome.scripting API (JS injection, 5-15ms)
    ├── chrome.debugger API (full CDP, 5-20ms, shows debug bar)
    └── Content scripts (DOM observation, persistent)
    │
    ▼
Any open Chrome tab (no flags, no restart)
```

### Extension Manifest (v3)

```json
{
  "manifest_version": 3,
  "name": "agent-eyes Bridge",
  "version": "1.0.0",
  "permissions": [
    "nativeMessaging",
    "tabs",
    "scripting",
    "activeTab"
  ],
  "optional_permissions": ["debugger"],
  "host_permissions": ["<all_urls>"],
  "background": { "service_worker": "background.js" }
}
```

Key decisions:
- `debugger` is **optional** — only requested when needed (avoids debug bar for 90% of ops)
- `chrome.scripting` handles DOM reading, JS execution, clicking, typing
- `chrome.tabs` handles tab CRUD (list, create, navigate, close)
- Native Messaging keeps service worker alive indefinitely

### Communication Protocol

```python
# Python side (agent-eyes server)
class ExtensionBridge:
    """Persistent connection to Chrome Extension via Native Messaging."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._msg_id = 0

    async def send(self, action: str, params: dict) -> dict:
        """Send command to extension, await response."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "action": action, "params": params}
        future = asyncio.get_event_loop().create_future()
        self._pending[self._msg_id] = future
        self._write_message(msg)
        return await asyncio.wait_for(future, timeout=30.0)
```

### Tool Mapping via Extension

| Tool | Extension API | Performance | Debug Bar? |
|------|--------------|-------------|------------|
| eyes_get_web_tree | chrome.scripting (inject DOM-to-AX-tree builder) | 10-50ms | No |
| eyes_navigate | chrome.tabs.update(tabId, {url}) | <5ms | No |
| eyes_new_tab | chrome.tabs.create({url}) + onUpdated listener | 10-50ms | No |
| eyes_close_tab | chrome.tabs.remove(tabId) | <5ms | No |
| eyes_evaluate | chrome.scripting.executeScript | 5-15ms | No |
| eyes_click (web) | chrome.scripting (inject click by selector/coords) | 10-20ms | No |
| eyes_type (web) | chrome.scripting (inject focus + input events) | 10-30ms | No |
| eyes_fill_form | chrome.scripting (inject per-field) | 10-50ms | No |
| eyes_list_chrome_tabs | chrome.tabs.query({}) | <1ms | No |
| eyes_drag | chrome.debugger (Input.dispatchMouseEvent) | 5-20ms | Yes |
| eyes_handle_dialog | chrome.debugger (Page.handleJavaScriptDialog) | 5-10ms | Yes |
| eyes_file_upload | chrome.debugger (DOM.setFileInputFiles) | 5-10ms | Yes |
| eyes_wait_for | chrome.scripting (MutationObserver polling) | Variable | No |

90% of operations use `chrome.scripting`/`chrome.tabs` — **no debug bar**.

### JS-Based Accessibility Tree Builder

Injected via `chrome.scripting.executeScript` to replace CDP's `Accessibility.getFullAXTree`:

```javascript
// Builds accessibility-like tree from DOM
function buildAccessibilityTree(root, maxDepth = 5) {
    const ROLE_MAP = {
        'A': 'link', 'BUTTON': 'button', 'INPUT': 'textbox',
        'SELECT': 'combobox', 'TEXTAREA': 'textbox',
        'H1': 'heading', 'H2': 'heading', 'H3': 'heading',
        'NAV': 'navigation', 'MAIN': 'main', 'IMG': 'img',
        'TABLE': 'table', 'FORM': 'form', 'DIALOG': 'dialog',
    };

    function getRole(el) {
        return el.getAttribute('role') || ROLE_MAP[el.tagName] || el.tagName.toLowerCase();
    }

    function getName(el) {
        return el.getAttribute('aria-label')
            || el.getAttribute('title')
            || el.getAttribute('alt')
            || el.textContent?.trim().substring(0, 100)
            || '';
    }

    function isInteractive(el) {
        const tag = el.tagName;
        if (['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return true;
        if (el.getAttribute('role')) return true;
        if (el.getAttribute('tabindex') !== null) return true;
        if (el.onclick || el.getAttribute('onclick')) return true;
        return false;
    }

    function walk(el, depth) {
        if (depth > maxDepth) return null;
        const rect = el.getBoundingClientRect();
        const node = {
            role: getRole(el),
            name: getName(el),
            bounds: [Math.round(rect.x), Math.round(rect.y),
                     Math.round(rect.width), Math.round(rect.height)],
            interactive: isInteractive(el),
            children: [],
        };
        if (el.value !== undefined && el.value !== '') node.value = el.value;
        if (el.checked !== undefined) node.checked = el.checked;
        if (el.disabled) node.disabled = true;

        for (const child of el.children) {
            const childNode = walk(child, depth + 1);
            if (childNode) node.children.push(childNode);
        }
        return node;
    }

    return walk(root || document.body, 0);
}
```

This replaces the 240-CDP-call enrichment with a **single JS injection** that returns the complete tree.

---

## Tier 2: CDP Direct (Persistent WebSocket + Flat Sessions)

For when Chrome IS running with `--remote-debugging-port` or chrome://inspect toggle.

### Architecture (Matches Playwright/Puppeteer/chromedp)

```python
class CDPConnection:
    """Single persistent WebSocket to Chrome browser endpoint."""

    def __init__(self):
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._sessions: dict[str, CDPSession] = {}  # sessionId -> session
        self._msg_id = 0
        self._browser_session = CDPSession(self, "")  # root session

    async def connect(self, browser_ws_url: str):
        """Connect once, keep alive for server lifetime."""
        self._ws = await websockets.connect(browser_ws_url)
        asyncio.create_task(self._read_loop())

        # Auto-attach to all targets with flat sessions
        await self._browser_session.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True
        })

    async def _read_loop(self):
        """Dispatch incoming messages to correct session."""
        async for raw in self._ws:
            msg = json.loads(raw)
            session_id = msg.get("sessionId", "")
            session = self._sessions.get(session_id, self._browser_session)
            session._on_message(msg)

    def _send_raw(self, session_id: str, method: str, params: dict) -> int:
        """Send command tagged with sessionId. Returns message ID."""
        self._msg_id += 1
        message = {"id": self._msg_id, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        asyncio.create_task(self._ws.send(json.dumps(message)))
        return self._msg_id


class CDPSession:
    """Per-tab session. Commands flow through shared connection."""

    def __init__(self, connection: CDPConnection, session_id: str):
        self._connection = connection
        self._session_id = session_id
        self._callbacks: dict[int, asyncio.Future] = {}
        self._domains_enabled: set[str] = set()

    async def send(self, method: str, params: dict = None) -> dict:
        """Send CDP command, await response."""
        msg_id = self._connection._send_raw(
            self._session_id, method, params or {}
        )
        future = asyncio.get_event_loop().create_future()
        self._callbacks[msg_id] = future
        return await asyncio.wait_for(future, timeout=30.0)

    async def enable_domain(self, domain: str):
        """Enable domain once per session (idempotent)."""
        if domain not in self._domains_enabled:
            await self.send(f"{domain}.enable")
            self._domains_enabled.add(domain)

    def _on_message(self, msg: dict):
        """Route response to pending callback or emit event."""
        if "id" in msg:
            future = self._callbacks.pop(msg["id"], None)
            if future and not future.done():
                future.set_result(msg.get("result", {}))
        # Events (no "id") can be handled via event listeners
```

### Key Improvements Over Current CDP

| Aspect | Current | Redesigned |
|--------|---------|------------|
| WebSocket connections | New per method call | **1 for entire server lifetime** |
| Domain enables per 10 ops | 10+ | **1 (cached)** |
| is_available() checks | 10-20 HTTP requests | **0 (connected = available)** |
| Tab list refreshes | 10 HTTP calls | **0 (auto-attach events)** |
| Enrichment CDP calls | 240 sequential | **Batched via asyncio.gather or JS injection** |
| Connection overhead per op | ~150ms | **~0ms** |

### Enrichment Optimization

Replace sequential enrichment with parallel batching:

```python
async def _enrich_tree_batched(self, session: CDPSession, elements: list[UIElement]):
    """Enrich all elements in parallel instead of sequentially."""
    await session.enable_domain("DOM")
    await session.enable_domain("CSS")

    async def enrich_one(el):
        try:
            box = await session.send("DOM.getBoxModel",
                {"backendNodeId": el.platform_ref})
            # ... extract bounds
        except Exception:
            pass

    # All 60 elements enriched in parallel
    await asyncio.gather(*[enrich_one(el) for el in elements[:60]])
```

Or better — skip enrichment entirely and use the JS-based tree builder from Tier 1.

### Reconnection Logic

```python
async def _ensure_connected(self):
    """Reconnect if WebSocket dropped."""
    if self._ws and self._ws.open:
        return
    # Re-discover port, reconnect, re-attach
    port = discover_cdp_port() or self._default_port
    browser_ws = await self._get_browser_ws_url(port)
    await self.connect(browser_ws)
```

---

## Tier 3: Native AX + OS Fallback (Always Available)

### Fix: Wire Existing Code as Fallbacks

The code already exists in `applescript.py` — it just needs to be connected:

| Broken Tool | Fallback Implementation | Source |
|-------------|------------------------|--------|
| eyes_get_web_tree | `get_page_accessibility_summary()` + `get_page_text_content()` in applescript.py → wrap as tree structure | Lines 301-425 |
| eyes_close_tab | AppleScript `close tab N of window M` | New, simple |
| eyes_fill_form | `shadow_type(selector, text)` per field | Lines 543-570 |
| eyes_wait_for | Poll via `shadow_read_interactive()` | Lines 571-604 |
| eyes_drag | OS input simulation (CGEvent mouse drag) | Extend input_sim.py |
| eyes_handle_dialog | AppleScript `execute javascript "confirm(...)"` workaround | New |
| eyes_file_upload | AppleScript file dialog interaction or `eyes_evaluate` workaround | Limited |

### JS-Based get_web_tree Fallback (Cross-Platform via AppleScript on macOS)

```python
async def _get_web_tree_fallback(self) -> str:
    """Build web accessibility tree via JS injection (no CDP needed)."""
    js_code = BUILD_ACCESSIBILITY_TREE_JS  # The JS from Tier 1 section

    # Try AppleScript JS execution (macOS)
    if _as and _as.is_available():
        result = _as.execute_javascript(js_code)
        if result:
            return self._format_tree(json.loads(result))

    return "ERROR: Cannot read web tree without CDP or Chrome extension."
```

### Lightweight eyes_context

Add a `fast=true` mode that skips full tree traversal:

```python
async def _handle_context(args: dict) -> str:
    fast = args.get("fast", False)

    if fast:
        # Just: active app + window title + focused element
        app = native_adapter.get_frontmost_app()
        focused = native_adapter.get_focused_element()
        return f"App: {app.name}\nWindow: {app.window_title}\nFocused: {focused.role} '{focused.name}'"

    # Full context (existing behavior)
    ...
```

---

## Skill Rewrite (SKILL.md)

### Smart Workflow Routing

Replace rigid 4-step workflow with intent-based routing:

```markdown
## Workflow

### Fast Path (direct URL/action given)
When the user gives you a URL or clear action:
1. **Act directly**: `eyes_navigate(url)` or `eyes_new_tab(url)`
2. **Verify if needed**: Check result message for success

### Standard Path (exploring/interacting with current page)
1. **Orient**: `eyes_context` (fast mode) — know which app is active
2. **Read**: `eyes_get_web_tree` (web) or `eyes_get_tree` (native)
3. **Act**: click, type, fill_form, etc.
4. **Verify**: Check result or `eyes_wait_for`

### New Tab Rule
When user asks to open something in a new tab:
→ ALWAYS use `eyes_new_tab(url)`, NEVER navigate the current tab
```

### Page Load Awareness

```markdown
## Page Load Detection

After navigation (`eyes_navigate` or `eyes_new_tab`):
- The tool waits for page load before returning
- The response includes page title and URL
- You do NOT need to call eyes_get_web_tree just to confirm the page loaded
- Only call eyes_get_web_tree when you need to READ the page content
```

---

## Build Order (Incremental)

### Phase 1: Quick Wins (Tier 3 fixes + Skill rewrite) — ~2-3 days
1. Wire AppleScript fallbacks for 7 broken tools
2. Add JS-based get_web_tree fallback via AppleScript
3. Add lightweight `eyes_context(fast=true)`
4. Fix `eyes_new_tab` to wait for page load
5. Rewrite SKILL.md with smart workflow routing + new tab guidance

### Phase 2: CDP Persistent Connection (Tier 2) — ~3-5 days
1. Implement `CDPConnection` class (single persistent WebSocket)
2. Implement `CDPSession` class (flat session management)
3. Add `Target.setAutoAttach` for auto tab discovery
4. Replace all `async with websockets.connect()` patterns
5. Add domain caching (enable once per session)
6. Add reconnection logic
7. Batch or eliminate enrichment

### Phase 3: Chrome Extension Bridge (Tier 1) — ~5-7 days
1. Build Chrome Extension (manifest v3, service worker)
2. Implement Native Messaging host in Python
3. Build JS-based accessibility tree builder (injected via chrome.scripting)
4. Wire extension bridge as Tier 1 in server dispatch
5. Add extension auto-detection and connection management
6. Build installer/setup flow

### Phase 4: Polish — ~2-3 days
1. Unified error messages with tier indication
2. `eyes_status` shows which tier is active
3. Performance metrics logging
4. Cross-platform testing (Windows UIA, Linux AT-SPI2)

---

## Performance Projections

### Click on Already-Open Web Page

| Scenario | Current | Phase 1 | Phase 2 | Phase 3 |
|----------|---------|---------|---------|---------|
| CDP available | ~150ms | ~150ms | **~5ms** | ~5ms |
| CDP unavailable (macOS) | ❌ Error | ~50ms (AX) | ~50ms (AX) | **~15ms** |
| CDP unavailable (Win/Linux) | ❌ Error | ❌ Error | ❌ Error | **~15ms** |

### Read Web Page Tree

| Scenario | Current | Phase 1 | Phase 2 | Phase 3 |
|----------|---------|---------|---------|---------|
| CDP available | ~2500ms | ~2500ms | **~200ms** | ~200ms |
| CDP unavailable (macOS) | ❌ Error | ~300ms (JS) | ~300ms (JS) | **~30ms** |
| CDP unavailable (Win/Linux) | ❌ Error | ❌ Error | ❌ Error | **~30ms** |

### Total Tool Calls for "Navigate to URL"

| Scenario | Current | After Redesign |
|----------|---------|---------------|
| Skill-mandated workflow | 4 calls (orient→read→navigate→verify) | **1 call** (navigate, done) |
| Open in new tab | 4+ calls (orient→list tabs→navigate current tab→read) | **1 call** (new_tab with URL) |

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Chrome Extension review process | Self-hosted (developer mode) initially; publish to Web Store later |
| Native Messaging cross-platform differences | Python stdlib handles stdin/stdout; manifest paths per OS |
| CDP WebSocket drops | Auto-reconnect with session re-attachment |
| AppleScript "Allow JS from Apple Events" disabled | Detect and show clear error with instructions |
| Extension not installed | Graceful fallback to Tier 2/3 |
| Breaking changes in Chrome APIs | Pin manifest version, test against Chrome stable + canary |
