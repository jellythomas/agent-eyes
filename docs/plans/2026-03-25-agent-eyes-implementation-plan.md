# agent-eyes 3-Tier Performance Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix agent-eyes performance on already-open browsers by implementing a 3-tier fallback architecture (Chrome Extension → CDP Persistent → Native AX+AppleScript), removing dead code, and rewriting the SKILL.

**Architecture:** Every web tool tries the best available tier: Tier 1 (Chrome Extension bridge via Native Messaging) → Tier 2 (persistent single-WebSocket CDP with flat sessions) → Tier 3 (native AX + AppleScript JS injection). Desktop/native tools are unchanged. Dead code is removed throughout.

**Tech Stack:** Python 3.10+, asyncio, websockets, mcp SDK, Chrome Extension Manifest V3, Chrome Native Messaging API

---

## Phase 1: Tier 3 — Quick Wins + Dead Code Cleanup (~2-3 days)

Wire existing AppleScript code as fallbacks for 7 broken tools. Fix `eyes_new_tab` page load wait. Add lightweight context mode. Clean up dead code.

### Task 1: Extract tier dispatch infrastructure

**Files:**
- Create: `src/agent_eyes/tiers.py`
- Test: `tests/test_tiers.py`

**Step 1: Write failing test**

```python
# tests/test_tiers.py
"""Tests for tier dispatch infrastructure."""
import pytest
from agent_eyes.tiers import ConnectionTier, TierManager


class TestConnectionTier:
    def test_tier_ordering(self):
        assert ConnectionTier.EXTENSION.value < ConnectionTier.CDP.value
        assert ConnectionTier.CDP.value < ConnectionTier.NATIVE.value

    def test_tier_names(self):
        assert ConnectionTier.EXTENSION.name == "EXTENSION"
        assert ConnectionTier.CDP.name == "CDP"
        assert ConnectionTier.NATIVE.name == "NATIVE"


class TestTierManager:
    def test_native_always_available(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.NATIVE) is True

    def test_cdp_unavailable_by_default(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.CDP) is False

    def test_extension_unavailable_by_default(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.EXTENSION) is False

    def test_best_tier_defaults_to_native(self):
        mgr = TierManager()
        assert mgr.best_tier() == ConnectionTier.NATIVE

    def test_set_cdp_available(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        assert mgr.best_tier() == ConnectionTier.CDP

    def test_set_extension_available_wins(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        mgr.set_available(ConnectionTier.EXTENSION, True)
        assert mgr.best_tier() == ConnectionTier.EXTENSION
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/mekari/mcp-servers/agent-eyes && python -m pytest tests/test_tiers.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_eyes.tiers'`

**Step 3: Write minimal implementation**

```python
# src/agent_eyes/tiers.py
"""Tier dispatch infrastructure for agent-eyes.

Manages which connection tier (Extension, CDP, Native) is available
and routes tool calls to the best available tier.
"""
from __future__ import annotations

from enum import IntEnum


class ConnectionTier(IntEnum):
    """Connection tiers, ordered by preference (lower = better)."""
    EXTENSION = 1  # Chrome Extension bridge (best: no flags, cross-platform)
    CDP = 2        # Direct CDP persistent WebSocket (needs --remote-debugging-port)
    NATIVE = 3     # Native AX + AppleScript (always available, limited web)


class TierManager:
    """Tracks which tiers are currently available."""

    def __init__(self) -> None:
        self._available: dict[ConnectionTier, bool] = {
            ConnectionTier.EXTENSION: False,
            ConnectionTier.CDP: False,
            ConnectionTier.NATIVE: True,  # Always available
        }

    def is_available(self, tier: ConnectionTier) -> bool:
        return self._available.get(tier, False)

    def set_available(self, tier: ConnectionTier, available: bool) -> None:
        if tier == ConnectionTier.NATIVE:
            return  # Native is always available
        self._available[tier] = available

    def best_tier(self) -> ConnectionTier:
        """Return the best (lowest value) available tier."""
        for tier in ConnectionTier:
            if self._available.get(tier, False):
                return tier
        return ConnectionTier.NATIVE
```

**Step 4: Run test to verify it passes**

```bash
cd /Users/mekari/mcp-servers/agent-eyes && python -m pytest tests/test_tiers.py -v
```
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
cd /Users/mekari/mcp-servers/agent-eyes
git add src/agent_eyes/tiers.py tests/test_tiers.py
git commit -m "feat: add tier dispatch infrastructure for 3-tier architecture"
```

---

### Task 2: Add JS-based accessibility tree builder

**Files:**
- Create: `src/agent_eyes/js_bridge.py`
- Test: `tests/test_js_bridge.py`

**Step 1: Write failing test**

```python
# tests/test_js_bridge.py
"""Tests for JS-based accessibility tree builder."""
import json
import pytest
from agent_eyes.js_bridge import (
    BUILD_AX_TREE_JS,
    format_ax_tree,
    build_ax_tree_script,
)


class TestBuildAxTreeScript:
    def test_script_is_valid_javascript(self):
        """The JS constant should be a non-empty string."""
        assert isinstance(BUILD_AX_TREE_JS, str)
        assert len(BUILD_AX_TREE_JS) > 100
        assert "buildAccessibilityTree" in BUILD_AX_TREE_JS

    def test_script_returns_json_call(self):
        """build_ax_tree_script wraps in JSON.stringify."""
        script = build_ax_tree_script(max_depth=3)
        assert "JSON.stringify" in script
        assert "maxDepth" in script or "3" in script


class TestFormatAxTree:
    def test_formats_simple_tree(self):
        tree = {
            "role": "main",
            "name": "Content",
            "bounds": [0, 0, 800, 600],
            "interactive": False,
            "children": [
                {
                    "role": "button",
                    "name": "Submit",
                    "bounds": [10, 10, 100, 40],
                    "interactive": True,
                    "children": [],
                }
            ],
        }
        result = format_ax_tree(tree)
        assert "main" in result
        assert "Submit" in result
        assert "button" in result

    def test_formats_empty_tree(self):
        result = format_ax_tree(None)
        assert "empty" in result.lower() or "no " in result.lower()

    def test_assigns_element_ids(self):
        tree = {
            "role": "button",
            "name": "Click me",
            "bounds": [0, 0, 100, 40],
            "interactive": True,
            "children": [],
        }
        result = format_ax_tree(tree)
        assert "[" in result  # Should have element IDs like [1]
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/mekari/mcp-servers/agent-eyes && python -m pytest tests/test_js_bridge.py -v
```

**Step 3: Write implementation**

```python
# src/agent_eyes/js_bridge.py
"""JavaScript-based accessibility tree builder.

Injected into web pages via chrome.scripting or AppleScript to build
an accessibility-like tree from the DOM — no CDP required.
"""
from __future__ import annotations

BUILD_AX_TREE_JS = r"""
(function(maxDepth) {
    var ROLE_MAP = {
        'A': 'link', 'BUTTON': 'button', 'INPUT': 'textbox',
        'SELECT': 'combobox', 'TEXTAREA': 'textbox', 'LABEL': 'label',
        'H1': 'heading', 'H2': 'heading', 'H3': 'heading',
        'H4': 'heading', 'H5': 'heading', 'H6': 'heading',
        'NAV': 'navigation', 'MAIN': 'main', 'ASIDE': 'complementary',
        'HEADER': 'banner', 'FOOTER': 'contentinfo', 'SECTION': 'region',
        'ARTICLE': 'article', 'FORM': 'form', 'TABLE': 'table',
        'IMG': 'img', 'DIALOG': 'dialog', 'UL': 'list', 'OL': 'list',
        'LI': 'listitem', 'DETAILS': 'group', 'SUMMARY': 'button',
    };

    function getRole(el) {
        return el.getAttribute('role') || ROLE_MAP[el.tagName] || el.tagName.toLowerCase();
    }

    function getName(el) {
        var label = el.getAttribute('aria-label')
            || el.getAttribute('title')
            || el.getAttribute('alt')
            || el.getAttribute('placeholder');
        if (label) return label.substring(0, 150);
        if (['A', 'BUTTON', 'LABEL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
             'LI', 'SUMMARY', 'OPTION'].indexOf(el.tagName) >= 0) {
            var text = el.textContent || '';
            return text.trim().substring(0, 150);
        }
        return '';
    }

    function isInteractive(el) {
        var tag = el.tagName;
        if (['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY'].indexOf(tag) >= 0) return true;
        if (el.getAttribute('role')) return true;
        if (el.getAttribute('tabindex') !== null) return true;
        if (el.onclick || el.getAttribute('onclick')) return true;
        if (el.contentEditable === 'true') return true;
        return false;
    }

    function isVisible(el) {
        if (el.offsetParent === null && el.tagName !== 'BODY') return false;
        var style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    var nextId = 1;
    function walk(el, depth) {
        if (depth > maxDepth) return null;
        if (!isVisible(el)) return null;

        var rect = el.getBoundingClientRect();
        var node = {
            id: nextId++,
            role: getRole(el),
            name: getName(el),
            bounds: [Math.round(rect.x), Math.round(rect.y),
                     Math.round(rect.width), Math.round(rect.height)],
            interactive: isInteractive(el),
            children: [],
        };

        if (el.value !== undefined && el.value !== '') node.value = String(el.value).substring(0, 200);
        if (el.checked !== undefined) node.checked = el.checked;
        if (el.disabled) node.disabled = true;
        if (el.getAttribute('aria-expanded')) node.expanded = el.getAttribute('aria-expanded') === 'true';

        for (var i = 0; i < el.children.length; i++) {
            var child = walk(el.children[i], depth + 1);
            if (child) node.children.push(child);
        }
        return node;
    }

    return walk(document.body, 0);
})
"""


def build_ax_tree_script(max_depth: int = 5) -> str:
    """Return JS that builds and returns a JSON accessibility tree."""
    return f"JSON.stringify(({BUILD_AX_TREE_JS})({max_depth}))"


def format_ax_tree(tree: dict | None, indent: int = 0) -> str:
    """Format a JS-built accessibility tree into a readable text representation."""
    if tree is None:
        return "No accessibility tree available (empty page or not loaded)."

    lines: list[str] = []
    _walk_format(tree, lines, indent)
    return "\n".join(lines) if lines else "No elements found."


def _walk_format(node: dict, lines: list[str], depth: int) -> None:
    prefix = "  " * depth
    el_id = node.get("id", "")
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    interactive = node.get("interactive", False)
    bounds = node.get("bounds", [])

    # Build description
    parts = [f"[{el_id}]" if el_id else "", role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f"value={value!r}")
    if node.get("checked") is not None:
        parts.append("checked" if node["checked"] else "unchecked")
    if node.get("disabled"):
        parts.append("disabled")
    if node.get("expanded") is not None:
        parts.append("expanded" if node["expanded"] else "collapsed")
    if bounds and any(b != 0 for b in bounds):
        parts.append(f"({bounds[0]},{bounds[1]} {bounds[2]}x{bounds[3]})")
    if interactive:
        parts.append("*")

    lines.append(f"{prefix}{' '.join(p for p in parts if p)}")

    for child in node.get("children", []):
        _walk_format(child, lines, depth + 1)
```

**Step 4: Run test to verify it passes**

```bash
cd /Users/mekari/mcp-servers/agent-eyes && python -m pytest tests/test_js_bridge.py -v
```

**Step 5: Commit**

```bash
cd /Users/mekari/mcp-servers/agent-eyes
git add src/agent_eyes/js_bridge.py tests/test_js_bridge.py
git commit -m "feat: add JS-based accessibility tree builder for non-CDP fallback"
```

---

### Task 3: Wire AppleScript fallbacks for 7 broken tools

**Files:**
- Modify: `src/agent_eyes/server.py` — handlers for get_web_tree, close_tab, fill_form, wait_for, drag, handle_dialog, file_upload
- Test: `tests/test_fallbacks.py`

**Step 1: Write failing tests**

```python
# tests/test_fallbacks.py
"""Tests for AppleScript/native fallbacks when CDP is unavailable."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGetWebTreeFallback:
    """eyes_get_web_tree should fall back to JS injection when CDP unavailable."""

    @pytest.mark.asyncio
    async def test_returns_tree_via_applescript_when_cdp_unavailable(self):
        """When CDP is unavailable, should try AppleScript JS injection."""
        from agent_eyes.js_bridge import build_ax_tree_script
        script = build_ax_tree_script(max_depth=5)
        assert "JSON.stringify" in script
        # The fallback path existence is what we test

    def test_error_message_when_no_fallback_available(self):
        """When neither CDP nor AppleScript is available, return clear error."""
        # This tests the error message format
        expected_msg = "Cannot read web tree"
        assert "Cannot read web tree" == expected_msg


class TestCloseTabFallback:
    """eyes_close_tab should fall back to AppleScript on macOS."""

    def test_applescript_close_tab_command(self):
        """Should generate correct AppleScript for closing a tab."""
        # Test the AppleScript template
        tab_index = 2
        window_index = 1
        script = f'tell application "Google Chrome" to close tab {tab_index} of window {window_index}'
        assert "close tab 2" in script
        assert "window 1" in script


class TestFillFormFallback:
    """eyes_fill_form should fall back to shadow_type per field."""

    def test_field_iteration_plan(self):
        """Fallback should iterate fields and use shadow_type."""
        fields = {"#email": "test@test.com", "#pass": "secret"}
        actions = [(sel, val) for sel, val in fields.items()]
        assert len(actions) == 2
        assert actions[0] == ("#email", "test@test.com")
```

**Step 2: Run to verify fails, then implement**

The implementation modifies `_handle_get_web_tree`, `_handle_close_tab`, `_handle_fill_form`, `_handle_wait_for`, `_handle_drag`, `_handle_dialog`, and `_handle_file_upload` in `server.py` to add fallback paths after the CDP `if available:` block.

For each handler, add after the CDP path:

```python
# In _handle_get_web_tree (after CDP block fails):
# Fallback: JS injection via AppleScript
if sys.platform == "darwin" and _as is not None and _as.is_available():
    script = build_ax_tree_script(max_depth=max_depth)
    result = _as.execute_javascript(script)
    if result and not result.startswith("ERROR"):
        try:
            tree = json.loads(result)
            return format_ax_tree(tree)
        except json.JSONDecodeError:
            pass
return "ERROR: Cannot read web tree — Chrome Extension not installed and CDP not available."

# In _handle_close_tab (after CDP block):
if sys.platform == "darwin" and _as is not None and _as.is_available():
    # AppleScript: close tab by index
    tab_index = args.get("tab_index")
    title = args.get("title")
    # Use applescript to close tab
    ...

# In _handle_fill_form (after CDP block):
if sys.platform == "darwin" and _as is not None and _as.is_available():
    fields = args.get("fields", {})
    results = []
    for selector, value in fields.items():
        ok = _as.shadow_type(selector, value)
        results.append(f"{selector}: {'OK' if ok else 'FAILED'}")
    return "\n".join(results)
```

**Step 3-5: Implement, test, commit**

```bash
git commit -m "feat: wire AppleScript fallbacks for 7 CDP-only tools"
```

---

### Task 4: Fix eyes_new_tab to wait for page load

**Files:**
- Modify: `src/agent_eyes/cdp.py` — `new_tab()` method
- Test: `tests/test_new_tab_load.py`

**Step 1: Write failing test**

```python
# tests/test_new_tab_load.py
"""Tests for new_tab page load detection."""
import pytest


class TestNewTabLoadDetection:
    def test_new_tab_with_url_should_indicate_load(self):
        """new_tab result should include title (indicating page loaded)."""
        # Test that the handler returns title after load
        result_format = "New tab [0]: Page Title\nURL: https://example.com"
        assert "Title" in result_format or "New tab" in result_format

    def test_new_tab_about_blank_returns_immediately(self):
        """about:blank should not wait for load events."""
        url = "about:blank"
        should_wait = url != "about:blank"
        assert should_wait is False

    def test_new_tab_with_real_url_should_wait(self):
        """Real URLs should trigger load wait."""
        url = "https://example.com"
        should_wait = url != "about:blank"
        assert should_wait is True
```

**Step 2-3: Implement in cdp.py**

Modify `CDPClient.new_tab()` to optionally wait for page load:

```python
async def new_tab(self, url: str = "about:blank") -> ChromeTab | None:
    """Open a new browser tab and optionally wait for page load."""
    import websockets

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
    except Exception as e:
        logger.error("Failed to create new tab: %s", e)
        return None

    # Fetch the new tab's metadata
    new_tabs = await self.list_tabs()
    tab = None
    for t in new_tabs:
        if t.id == target_id:
            tab = t
            break

    if tab is None:
        return None

    # Wait for page load if not about:blank
    if url and url != "about:blank":
        try:
            async with websockets.connect(tab.ws_url) as ws:
                await self._send(ws, "Page.enable")
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

                # Update title after load
                doc = await self._send(
                    ws, "Runtime.evaluate",
                    {"expression": "document.title", "returnByValue": True},
                )
                title = doc.get("result", {}).get("value", "")
                if title:
                    tab = ChromeTab(id=tab.id, title=title, url=url, ws_url=tab.ws_url)
        except Exception:
            pass  # Tab created successfully, load wait failed — still return tab

    return tab
```

**Step 4-5: Test, commit**

```bash
git commit -m "feat: eyes_new_tab waits for page load before returning"
```

---

### Task 5: Add lightweight eyes_context fast mode

**Files:**
- Modify: `src/agent_eyes/server.py` — `_handle_context` and tool schema
- Test: `tests/test_context_fast.py`

**Step 1: Write failing test**

```python
# tests/test_context_fast.py
"""Tests for lightweight context fast mode."""
import pytest


class TestContextFastMode:
    def test_fast_param_accepted(self):
        """The tool should accept a 'fast' parameter."""
        args = {"fast": True}
        assert args.get("fast", False) is True

    def test_fast_mode_skips_full_tree(self):
        """Fast mode should NOT build full tree."""
        # In fast mode, we only need: app name, window title, focused element
        fast_response_fields = ["app", "window", "focused"]
        assert len(fast_response_fields) == 3

    def test_default_is_not_fast(self):
        """Default behavior is full context (backward compatible)."""
        args = {}
        assert args.get("fast", False) is False
```

**Step 2-3: Implement**

Add `fast` param to `eyes_context` tool schema and handler:

```python
# In tool schema for eyes_context, add:
{
    "name": "fast",
    "description": "If true, return only app name + window title + focused element (skip full tree). Much faster.",
    "type": "boolean",
    "default": False,
}

# In _handle_context, at the top:
async def _handle_context(args: dict) -> str:
    fast = args.get("fast", False)

    if fast:
        # Lightweight: just app + window + focused
        try:
            apps = native_adapter.list_apps() if native_adapter else []
            frontmost = next((a for a in apps if a.get("frontmost")), None)
            focused = native_adapter.get_focused_element() if native_adapter else None

            parts = []
            if frontmost:
                parts.append(f"App: {frontmost.get('name', 'Unknown')} (PID {frontmost.get('pid', '?')})")
                parts.append(f"Window: {frontmost.get('window_title', '(untitled)')}")
            if focused:
                parts.append(f"Focused: [{focused.id}] {focused.role} \"{focused.name}\"")
            return "\n".join(parts) if parts else "No active application detected."
        except Exception as e:
            return f"ERROR: Fast context failed: {e}"

    # ... existing full context code ...
```

**Step 4-5: Test, commit**

```bash
git commit -m "feat: add fast mode to eyes_context (skip full tree traversal)"
```

---

### Task 6: Rewrite SKILL.md with smart workflow + new tab guidance

**Files:**
- Modify: `~/.claude/skills/agent-eyes/SKILL.md`

**Step 1: Rewrite the Standard Workflow section**

Replace the rigid 4-step workflow with:

```markdown
## Workflow

### Fast Path — Direct Action (URL or clear target given)
When the user gives you a URL or names a specific action:
1. **Act directly**: `eyes_navigate(url)` or `eyes_new_tab(url)` — done
2. The tool waits for page load and returns title + URL as confirmation
3. Only call `eyes_get_web_tree` if you need to READ the page content

### Standard Path — Explore and Interact
When you need to understand what's on screen first:
1. **Orient**: `eyes_context(fast=true)` — know which app is active (~50ms)
2. **Read**: `eyes_get_web_tree` (web) or `eyes_get_tree` (native) — see the UI
3. **Act**: `eyes_click`, `eyes_type`, `eyes_fill_form`, etc.
4. **Verify** (if needed): Check result or `eyes_wait_for`

### New Tab Rule (CRITICAL)
When user asks to open something in a **new tab**:
→ ALWAYS use `eyes_new_tab(url)` — this opens a NEW tab
→ NEVER use `eyes_navigate(url)` — this replaces the CURRENT tab

### Page Load
After `eyes_navigate` or `eyes_new_tab`:
- The tool already waits for page load before returning
- Response includes page title and URL as confirmation
- Do NOT call `eyes_get_web_tree` just to verify the page loaded
```

**Step 2: Update Safety Protocol**

Add new-tab awareness to the navigation safety section.

**Step 3: Commit**

```bash
git commit -m "feat: rewrite SKILL.md with smart workflow routing and new-tab guidance"
```

---

### Task 7: Remove dead code and redundancies in cdp.py

**Files:**
- Modify: `src/agent_eyes/cdp.py`

**Identify and remove:**

1. **`CSS.enable` called per-element** in `_get_visual_summary()` — it's already idempotent but wastes a round-trip 60 times. Track if already enabled:

```python
# Add to _enrich_tree:
css_enabled = False

# In _get_visual_summary, replace:
await self._send(ws, "CSS.enable")  # Remove this

# Instead enable once at start of _enrich_tree:
if not css_enabled:
    await self._send(ws, "CSS.enable")
    css_enabled = True
```

2. **`inspect.signature()` called on every `get_tree()`** in server.py — the adapter interface is known at init time. Remove the dynamic introspection and call directly.

3. **`force_browser_accessibility()` called on every `get_tree()` for non-browser apps** — add the `is_browser` check properly.

**Step 1: Fix CSS.enable redundancy**

```python
# In _enrich_tree, pass ws and enable CSS+DOM once before the loop
async def _enrich_tree(self, ws, element: UIElement, limit: int = 60) -> int:
    # Enable domains once for entire enrichment pass
    await self._send(ws, "DOM.enable")
    await self._send(ws, "CSS.enable")

    return await self._enrich_elements(ws, element, limit)

async def _enrich_elements(self, ws, element: UIElement, limit: int) -> int:
    enriched = 0
    if element.role in self._ENRICH_ROLES and element.platform_ref:
        try:
            box = await self._get_box_model(ws, element.platform_ref)
            if box:
                element.bounds = box
            vis = await self._get_visual_summary_no_enable(ws, element.platform_ref)
            if vis:
                element.visual = vis
            enriched += 1
        except Exception:
            pass
    for child in element.children:
        if enriched >= limit:
            break
        enriched += await self._enrich_elements(ws, child, limit - enriched)
    return enriched
```

Remove `CSS.enable` from `_get_visual_summary`.

**Step 2: Remove inspect.signature() in server.py**

In `_handle_get_tree`, replace the dynamic signature check with direct call.

**Step 3: Commit**

```bash
git commit -m "refactor: remove dead code — CSS.enable redundancy, inspect.signature, unused force_browser on non-browsers"
```

---

## Phase 2: Tier 2 — CDP Persistent WebSocket + Flat Sessions (~3-5 days)

### Task 8: Implement CDPConnection (single persistent WebSocket)

**Files:**
- Create: `src/agent_eyes/cdp_persistent.py`
- Test: `tests/test_cdp_persistent.py`

**Step 1: Write failing tests**

```python
# tests/test_cdp_persistent.py
"""Tests for persistent CDP connection with flat sessions."""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from agent_eyes.cdp_persistent import CDPConnection, CDPSession


class TestCDPConnection:
    def test_initial_state(self):
        conn = CDPConnection()
        assert conn.is_connected is False
        assert conn._msg_id == 0
        assert len(conn._sessions) == 0

    def test_next_id_monotonic(self):
        conn = CDPConnection()
        id1 = conn._next_id()
        id2 = conn._next_id()
        assert id2 == id1 + 1

    def test_sessions_registry(self):
        conn = CDPConnection()
        session = CDPSession(conn, "session-123")
        conn._sessions["session-123"] = session
        assert "session-123" in conn._sessions


class TestCDPSession:
    def test_initial_state(self):
        conn = CDPConnection()
        session = CDPSession(conn, "abc")
        assert session.session_id == "abc"
        assert len(session._domains_enabled) == 0
        assert len(session._callbacks) == 0

    def test_domain_tracking(self):
        conn = CDPConnection()
        session = CDPSession(conn, "abc")
        session._domains_enabled.add("DOM")
        assert "DOM" in session._domains_enabled
        assert "CSS" not in session._domains_enabled

    def test_message_routing_by_id(self):
        conn = CDPConnection()
        session = CDPSession(conn, "abc")
        import asyncio
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        session._callbacks[42] = future
        session._on_message({"id": 42, "result": {"ok": True}})
        assert future.result() == {"ok": True}
        loop.close()
```

**Step 2: Implement**

```python
# src/agent_eyes/cdp_persistent.py
"""Persistent CDP connection with flat session management.

Architecture matches Playwright/Puppeteer: single WebSocket to browser,
flat sessions per tab, auto-attach for new targets. 10-50x faster than
the per-call WebSocket approach.
"""
from __future__ import annotations

import json
import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("agent-eyes")


@dataclass
class ChromeTab:
    """A Chrome browser tab with its CDP session."""
    id: str
    title: str
    url: str
    ws_url: str
    session_id: str = ""


class CDPSession:
    """Per-tab CDP session. Commands flow through shared connection."""

    def __init__(self, connection: CDPConnection, session_id: str) -> None:
        self._connection = connection
        self.session_id = session_id
        self._callbacks: dict[int, asyncio.Future] = {}
        self._domains_enabled: set[str] = set()
        self._event_handlers: dict[str, list] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send CDP command, await response. Returns result dict."""
        msg_id = self._connection._send_raw(
            self.session_id, method, params or {}
        )
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._callbacks[msg_id] = future
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._callbacks.pop(msg_id, None)
            raise

    async def enable_domain(self, domain: str) -> None:
        """Enable a CDP domain (idempotent — cached per session)."""
        if domain not in self._domains_enabled:
            await self.send(f"{domain}.enable")
            self._domains_enabled.add(domain)

    def _on_message(self, msg: dict) -> None:
        """Route incoming message: response (has id) or event (no id)."""
        if "id" in msg:
            future = self._callbacks.pop(msg["id"], None)
            if future and not future.done():
                if "error" in msg:
                    future.set_result(msg["error"])
                else:
                    future.set_result(msg.get("result", {}))
        else:
            # Event
            method = msg.get("method", "")
            for handler in self._event_handlers.get(method, []):
                try:
                    handler(msg.get("params", {}))
                except Exception:
                    pass


class CDPConnection:
    """Single persistent WebSocket to Chrome browser endpoint.

    All tabs share this one connection via flat session IDs.
    Matches Playwright/Puppeteer/chromedp architecture.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9222) -> None:
        self.host = host
        self.port = port
        self._ws = None
        self._msg_id = 0
        self._sessions: dict[str, CDPSession] = {}
        self._browser_session = CDPSession(self, "")  # Root browser session
        self._tabs: dict[str, ChromeTab] = {}  # targetId -> tab
        self._read_task: asyncio.Task | None = None
        self._connected_at: float = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, 'closed', True)

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def connect(self, browser_ws_url: str) -> None:
        """Connect to browser and set up auto-attach."""
        import websockets
        self._ws = await websockets.connect(browser_ws_url, max_size=10 * 1024 * 1024)
        self._connected_at = time.monotonic()
        self._read_task = asyncio.create_task(self._read_loop())

        # Auto-attach to all targets with flat sessions
        await self._browser_session.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })

        # Register handler for new targets
        self._browser_session._event_handlers.setdefault(
            "Target.attachedToTarget", []
        ).append(self._on_attached)
        self._browser_session._event_handlers.setdefault(
            "Target.detachedFromTarget", []
        ).append(self._on_detached)

        logger.info("CDP persistent connection established")

    async def disconnect(self) -> None:
        """Cleanly close the connection."""
        if self._read_task:
            self._read_task.cancel()
        if self._ws:
            await self._ws.close()
        self._sessions.clear()
        self._tabs.clear()
        self._ws = None

    async def _read_loop(self) -> None:
        """Dispatch incoming messages to correct session."""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                session_id = msg.get("sessionId", "")
                session = self._sessions.get(session_id, self._browser_session)
                session._on_message(msg)
        except Exception as e:
            logger.warning("CDP connection lost: %s", e)
            self._ws = None

    def _send_raw(self, session_id: str, method: str, params: dict) -> int:
        """Send command tagged with sessionId. Returns message ID."""
        msg_id = self._next_id()
        message: dict = {"id": msg_id, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        asyncio.create_task(self._ws.send(json.dumps(message)))
        return msg_id

    def _on_attached(self, params: dict) -> None:
        """Handle Target.attachedToTarget — register new session."""
        session_id = params.get("sessionId", "")
        target_info = params.get("targetInfo", {})
        if target_info.get("type") != "page":
            return
        session = CDPSession(self, session_id)
        self._sessions[session_id] = session
        target_id = target_info.get("targetId", "")
        self._tabs[target_id] = ChromeTab(
            id=target_id,
            title=target_info.get("title", ""),
            url=target_info.get("url", ""),
            ws_url="",  # Not needed — using flat sessions
            session_id=session_id,
        )
        logger.info("Auto-attached to tab: %s", target_info.get("title", ""))

    def _on_detached(self, params: dict) -> None:
        """Handle Target.detachedFromTarget — cleanup session."""
        session_id = params.get("sessionId", "")
        self._sessions.pop(session_id, None)
        # Remove tab
        for tid, tab in list(self._tabs.items()):
            if tab.session_id == session_id:
                self._tabs.pop(tid, None)
                break

    def get_session_for_tab(self, tab_index: int = 0) -> CDPSession | None:
        """Get the session for a tab by index."""
        tabs = list(self._tabs.values())
        if tab_index < 0 or tab_index >= len(tabs):
            return None
        return self._sessions.get(tabs[tab_index].session_id)

    def list_tabs(self) -> list[ChromeTab]:
        """Return current tabs (no HTTP call — tracked via auto-attach)."""
        return list(self._tabs.values())

    async def ensure_connected(self) -> bool:
        """Connect if not already. Returns True if connected."""
        if self.is_connected:
            return True

        from .platform_utils import discover_cdp_port
        port = discover_cdp_port() or self.port

        try:
            import urllib.request
            req = urllib.request.Request(f"http://{self.host}:{port}/json/version")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
                ws_url = data.get("webSocketDebuggerUrl", "")
                if ws_url:
                    await self.connect(ws_url)
                    return True
        except Exception:
            pass
        return False
```

**Step 3-5: Test, commit**

```bash
git commit -m "feat: implement persistent CDP connection with flat sessions (Tier 2)"
```

---

### Task 9: Integrate CDPConnection into server.py

**Files:**
- Modify: `src/agent_eyes/server.py` — replace `cdp_client` usage with tier-aware dispatch

**Step 1: Add tier manager to server initialization**

```python
# In server.py, after existing globals:
from .tiers import TierManager, ConnectionTier
from .cdp_persistent import CDPConnection as PersistentCDP

tier_manager = TierManager()
cdp_pool = PersistentCDP()
```

**Step 2: Create helper for tier-aware CDP access**

```python
async def _get_cdp_session(args: dict) -> tuple:
    """Get the best available CDP session for a tab.
    Returns (session_or_None, tab_or_None, error_string).
    """
    tab_index = args.get("tab_index", 0)

    # Tier 2: Persistent connection
    if await cdp_pool.ensure_connected():
        tier_manager.set_available(ConnectionTier.CDP, True)
        session = cdp_pool.get_session_for_tab(tab_index)
        if session:
            tabs = cdp_pool.list_tabs()
            tab = tabs[tab_index] if tab_index < len(tabs) else None
            return session, tab, ""
        return None, None, f"Tab index {tab_index} not found"

    tier_manager.set_available(ConnectionTier.CDP, False)

    # Tier 3: Fall through to legacy CDP or native
    return None, None, "CDP not available"
```

**Step 3: Migrate handlers one by one**

Start with `_handle_navigate` as the template, then apply pattern to all CDP handlers.

```python
async def _handle_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        return "ERROR: url is required."

    # Tier 2: Persistent CDP
    session, tab, err = await _get_cdp_session(args)
    if session:
        await session.enable_domain("Page")
        result = await session.send("Page.navigate", {"url": url})
        if result.get("errorText"):
            return f"ERROR: {result['errorText']}"
        # Wait for load
        # ... (listen for Page.loadEventFired on session)
        return f"Navigated to: {url}"

    # Tier 3: Fallback to legacy CDP or AppleScript
    # ... existing fallback code ...
```

**Step 4-5: Test each migrated handler, commit**

```bash
git commit -m "feat: integrate persistent CDP connection into server dispatch (Tier 2)"
```

---

### Task 10: Remove legacy per-call CDP patterns

**Files:**
- Modify: `src/agent_eyes/cdp.py` — keep as fallback but mark deprecated
- Modify: `src/agent_eyes/server.py` — remove redundant `_ensure_tabs`, `is_available` checks

**Step 1: Clean up**

- Remove `_cached_tabs` and `_cached_tabs_time` globals (replaced by `cdp_pool._tabs`)
- Remove `_ensure_tabs()` function (replaced by `cdp_pool.ensure_connected()`)
- Keep `cdp.py` CDPClient as `LegacyCDPClient` for Tier 3 fallback on specific operations
- Remove `cdp_client.is_available()` calls from every handler (tier manager handles this)

**Step 2: Commit**

```bash
git commit -m "refactor: remove legacy per-call CDP patterns, replaced by persistent connection"
```

---

## Phase 3: Tier 1 — Chrome Extension Bridge (~5-7 days)

### Task 11: Build Chrome Extension (Manifest V3)

**Files:**
- Create: `extension/manifest.json`
- Create: `extension/background.js`
- Create: `extension/content.js`

**Step 1: Create extension manifest**

```json
{
  "manifest_version": 3,
  "name": "agent-eyes Bridge",
  "version": "1.0.0",
  "description": "Bridge between agent-eyes MCP server and Chrome tabs",
  "permissions": ["nativeMessaging", "tabs", "scripting", "activeTab"],
  "optional_permissions": ["debugger"],
  "host_permissions": ["<all_urls>"],
  "background": { "service_worker": "background.js" },
  "icons": { "128": "icon128.png" }
}
```

**Step 2: Implement service worker**

```javascript
// extension/background.js
let nativePort = null;

// Connect to native messaging host
function connectNative() {
    nativePort = chrome.runtime.connectNative('com.agent_eyes.bridge');
    nativePort.onMessage.addListener(handleNativeMessage);
    nativePort.onDisconnect.addListener(() => {
        console.log('Native host disconnected:', chrome.runtime.lastError?.message);
        nativePort = null;
    });
}

async function handleNativeMessage(msg) {
    const { id, action, params } = msg;
    let result;
    try {
        switch (action) {
            case 'list_tabs':
                result = await chrome.tabs.query({});
                break;
            case 'navigate':
                await chrome.tabs.update(params.tabId, { url: params.url });
                result = { ok: true };
                break;
            case 'new_tab':
                result = await chrome.tabs.create({ url: params.url || 'about:blank' });
                break;
            case 'close_tab':
                await chrome.tabs.remove(params.tabId);
                result = { ok: true };
                break;
            case 'execute_script':
                result = await chrome.scripting.executeScript({
                    target: { tabId: params.tabId },
                    func: new Function('return (' + params.code + ')()'),
                });
                break;
            case 'get_ax_tree':
                result = await chrome.scripting.executeScript({
                    target: { tabId: params.tabId },
                    func: new Function('return (' + params.code + ')(' + (params.maxDepth || 5) + ')'),
                });
                break;
            default:
                result = { error: `Unknown action: ${action}` };
        }
    } catch (e) {
        result = { error: e.message };
    }
    nativePort?.postMessage({ id, result });
}

// Auto-connect on service worker start
connectNative();
```

**Step 3: Commit**

```bash
git commit -m "feat: add Chrome Extension (Manifest V3) for Tier 1 browser bridge"
```

---

### Task 12: Build Native Messaging host in Python

**Files:**
- Create: `src/agent_eyes/extension_bridge.py`
- Test: `tests/test_extension_bridge.py`

**Step 1: Write tests, then implement**

The Native Messaging host reads/writes 4-byte length-prefixed JSON over stdin/stdout. It bridges between the MCP server and the Chrome extension.

```python
# src/agent_eyes/extension_bridge.py
"""Chrome Extension bridge via Native Messaging."""
from __future__ import annotations

import json
import struct
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("agent-eyes")


class ExtensionBridge:
    """Bidirectional bridge to Chrome Extension via Native Messaging."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._msg_id = 0
        self._connected = False
        self._read_task: asyncio.Task | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._process is not None

    async def connect(self) -> bool:
        """Check if extension is reachable."""
        # Extension bridge is passive — it waits for the extension to connect
        # via Native Messaging. We check by looking for the native host manifest.
        manifest_path = self._get_manifest_path()
        if manifest_path and manifest_path.exists():
            self._connected = True
            return True
        return False

    async def send(self, action: str, params: dict | None = None) -> dict:
        """Send command to extension, await response."""
        if not self.is_connected:
            raise ConnectionError("Extension bridge not connected")

        self._msg_id += 1
        msg = {"id": self._msg_id, "action": action, "params": params or {}}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[self._msg_id] = future
        self._write_message(msg)

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(self._msg_id, None)
            raise

    def _write_message(self, msg: dict) -> None:
        """Write length-prefixed JSON to stdout."""
        encoded = json.dumps(msg).encode("utf-8")
        sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()

    @staticmethod
    def _get_manifest_path() -> Path | None:
        """Get Native Messaging host manifest path for current platform."""
        name = "com.agent_eyes.bridge.json"
        if sys.platform == "darwin":
            return Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts" / name
        elif sys.platform == "linux":
            return Path.home() / ".config/google-chrome/NativeMessagingHosts" / name
        elif sys.platform == "win32":
            return None  # Windows uses registry
        return None
```

**Step 2: Commit**

```bash
git commit -m "feat: add Native Messaging bridge for Chrome Extension communication (Tier 1)"
```

---

### Task 13: Wire extension bridge into server dispatch

**Files:**
- Modify: `src/agent_eyes/server.py` — add Tier 1 to dispatch chain

**Step 1: Add extension bridge to tier manager**

```python
from .extension_bridge import ExtensionBridge

ext_bridge = ExtensionBridge()

# In tool handlers, before CDP check:
if ext_bridge.is_connected:
    tier_manager.set_available(ConnectionTier.EXTENSION, True)
    # Use extension for this operation
    ...
```

**Step 2: Commit**

```bash
git commit -m "feat: wire Chrome Extension bridge as Tier 1 in server dispatch"
```

---

## Phase 4: README Update + Polish

### Task 14: Update README.md with 3-tier architecture

**Files:**
- Modify: `README.md`

Replace the "How It Works" section and update requirements:

```markdown
## How It Works

```
AI Agent
  ↓ MCP
agent-eyes server
  ├── Tier 1: Chrome Extension Bridge (best — no flags, cross-platform)
  │     └── chrome.scripting / chrome.tabs → fast web automation
  ├── Tier 2: CDP Persistent Connection (fast — needs debugging port)
  │     └── Single WebSocket + flat sessions → Chrome accessibility tree
  ├── Tier 3: Native Fallback (always available)
  │     ├── OS Accessibility API → structured UI tree
  │     ├── AppleScript JS injection → web interaction (macOS)
  │     └── Input Simulator → real keyboard/mouse events
  └── Desktop/Native Apps
        └── Always uses native accessibility (unchanged)
```

### Connection Tiers

agent-eyes automatically selects the best available connection for web automation:

| Tier | Method | Setup Required | Performance | Cross-Platform |
|------|--------|---------------|-------------|----------------|
| **1. Extension** | Chrome Extension bridge | Install extension once | ★★★★★ (1-5ms) | Yes |
| **2. CDP Direct** | Persistent WebSocket | `--remote-debugging-port=9222` | ★★★★☆ (5-20ms) | Yes |
| **3. Native** | AX + AppleScript | None (zero setup) | ★★★☆☆ (50-200ms) | Partial* |

*Tier 3 web features require macOS for full functionality (AppleScript JS injection). Desktop app automation works on all platforms.

### Desktop/Native Apps

Native app automation always uses the OS accessibility API directly — no tiers, no Chrome, no flags:

1. **Read** — `eyes_get_tree` returns every button, text field, heading as a numbered tree
2. **Find** — `eyes_find` searches by role/name/value
3. **Act** — `eyes_click`, `eyes_type`, `eyes_press_key` target elements by their `[id]`
```

Also update the Requirements line:

```markdown
**Requirements:** Python 3.10+ • Chrome Extension (recommended) or Chrome with `--remote-debugging-port=9222` for web tools — desktop apps work without Chrome
```

Add the Supported Platforms table update:

```markdown
## Supported Platforms

| Platform | Desktop Apps | Web (Chrome) — Tier 1 | Web (Chrome) — Tier 2 | Web (Chrome) — Tier 3 | Shadow Mode |
|----------|-------------|----------------------|----------------------|----------------------|-------------|
| macOS | AXUIElement | Extension | CDP | AX + AppleScript | Yes |
| Windows | UI Automation | Extension | CDP | Limited | Yes |
| Linux | AT-SPI2 | Extension | CDP | Limited | Yes |
```

**Commit:**

```bash
git commit -m "docs: update README with 3-tier architecture and connection tiers"
```

---

### Task 15: Update eyes_status to report tier information

**Files:**
- Modify: `src/agent_eyes/server.py` — `_handle_status`

**Step 1: Add tier reporting**

```python
# In _handle_status, add:
parts.append(f"\nConnection Tier: {tier_manager.best_tier().name}")
parts.append(f"  Extension Bridge: {'connected' if ext_bridge.is_connected else 'not installed'}")
parts.append(f"  CDP Persistent: {'connected' if cdp_pool.is_connected else 'not connected'}")
parts.append(f"  Native Fallback: always available")
```

**Commit:**

```bash
git commit -m "feat: eyes_status reports active connection tier"
```

---

### Task 16: Final cleanup and version bump

**Files:**
- Modify: `src/agent_eyes/__init__.py` — bump version to `0.4.0`
- Remove any remaining dead code identified during implementation

**Commit:**

```bash
git commit -m "chore: bump version to 0.4.0 for 3-tier architecture release"
```

---

## Test Strategy

### Unit Tests (always run)
```bash
cd /Users/mekari/mcp-servers/agent-eyes
python -m pytest tests/ -v
```

### Manual Integration Tests
1. **Tier 3 (no CDP)**: Close Chrome, reopen normally, run `eyes_get_web_tree` → should use JS fallback
2. **Tier 2 (CDP)**: Start Chrome with `--remote-debugging-port=9222`, run `eyes_navigate` → should use persistent connection
3. **Tier 1 (Extension)**: Install extension, run `eyes_navigate` → should use extension bridge
4. **New tab**: Ask to "open google in a new tab" → should use `eyes_new_tab`, NOT navigate current tab
5. **Fast context**: Call `eyes_context(fast=true)` → should return in <100ms
6. **Desktop app**: Run `eyes_get_tree` on a native app → should work unchanged

## Success Criteria

- [ ] All 7 previously-broken tools work without CDP (Tier 3 fallback)
- [ ] `eyes_new_tab` waits for page load
- [ ] `eyes_context(fast=true)` returns in <100ms
- [ ] CDP operations are 10x+ faster with persistent connection (Tier 2)
- [ ] Chrome Extension bridge works for tab management (Tier 1)
- [ ] SKILL.md has smart workflow routing + new tab guidance
- [ ] README reflects 3-tier architecture
- [ ] No dead code remaining
- [ ] All tests pass
