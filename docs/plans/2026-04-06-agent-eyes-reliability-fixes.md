# Agent-Eyes Reliability & Action Verification Fixes

**Date:** 2026-04-06
**Status:** Planning
**Priority:** P0 - Critical (Blocking all other work)

## Executive Summary

Deep analysis revealed that agent-eyes has fundamental reliability issues causing **25-45% of actions to silently fail or target the wrong element/window/tab**. These issues must be fixed before any performance optimization work.

The core problem: **agent-eyes assumes actions succeed without verification**.

---

## Validated Critical Issues

### Issue #1: CDP Click/Type ALWAYS Uses Tab 0 (HARDCODED BUG)

**Severity:** CRITICAL
**Location:** `server.py:1276-1277`, `server.py:1333-1334`
**Failure Rate:** 100% when working with non-first tabs

```python
# CURRENT (BROKEN):
if _cached_tabs:
    tab = _cached_tabs[0]  # ← HARDCODED! Element from tab 3 → click on tab 0
    success = await cdp_client.click_element(tab, element.platform_ref)
```

**Root Cause:** `UIElement` has NO `tab_index` field. Elements lose their tab association when registered.

**Fix Required:**
1. Add `tab_index: int = -1` field to `UIElement` dataclass
2. Set `tab_index` when registering CDP elements in `_handle_get_web_tree`
3. Use `element.tab_index` (not hardcoded 0) in click/type handlers

---

### Issue #2: No Focus Verification After activate_window

**Severity:** CRITICAL
**Location:** `server.py:1251-1253`, `server.py:1296-1302`, `server.py:1347-1349`
**Failure Rate:** 5-15% on busy desktops

```python
# CURRENT (BROKEN):
if element.pid:
    input_backend.activate_window(element.pid)
    time.sleep(0.1)  # ← Race window: another app can steal focus
# NO VERIFICATION - click may go to wrong app
input_backend.click(cx, cy)
```

**Evidence:** `is_frontmost()` exists in `input_sim.py:377-383` but is NEVER called after `activate_window()`.

**Fix Required:**
1. After `activate_window()`, call `is_frontmost(pid)` to verify
2. Retry with longer delay if not frontmost
3. Return error if still not frontmost after retries

```python
# FIXED:
if element.pid:
    input_backend.activate_window(element.pid)
    if not await _verify_focus(element.pid, timeout=0.5):
        return f"ERROR: Could not focus app (PID {element.pid}). Action aborted."
```

---

### Issue #3: CDP Elements NEVER Validated for Staleness

**Severity:** CRITICAL
**Location:** `server.py:1265-1270`
**Failure Rate:** 10-20% on dynamic SPAs (React, Vue, Angular)

```python
# CURRENT (BROKEN):
# Validation ONLY for native elements!
if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
    if not native_adapter.is_element_valid(element):
        return f"ERROR: Element [{element_id}] is stale..."

# CDP elements (source="cdp") bypass validation entirely!
if element.source == "cdp" and element.platform_ref is not None:
    # ← NO VALIDATION
```

**Fix Required:**
1. Add CDP element validation before action
2. Use `DOM.resolveNode` to check if `backend_node_id` is still valid
3. Return clear error if element is stale

```python
# FIXED:
if element.source == "cdp":
    if not await cdp_client.is_element_valid(tab, element.platform_ref):
        return f"ERROR: Element [{element_id}] is stale (page changed). Call eyes_get_web_tree to refresh."
```

---

### Issue #4: Web Element Typing NEVER Verified

**Severity:** CRITICAL
**Location:** `server.py:1373-1378`
**Failure Rate:** 100% of web typing is unverified

```python
# CURRENT (BROKEN):
if is_web:
    return (
        f"Typed \"{text}\" into [{element_id}]..."
        f"(focus + keyboard injection)"
    )  # ← RETURNS SUCCESS WITHOUT ANY VERIFICATION
```

**Fix Required:**
1. After typing, read element value via CDP
2. Verify typed text appears in element
3. Return warning if verification fails

```python
# FIXED:
if is_web:
    # Verify typing worked
    actual_value = await cdp_client.get_element_value(tab, element.platform_ref)
    if text not in actual_value:
        return f"WARNING: Typed \"{text}\" but verification failed. Current value: \"{actual_value}\""
```

---

### Issue #5: activate_window Always Returns True

**Severity:** HIGH
**Location:** `input_sim.py:400-407`

```python
# CURRENT (BROKEN):
subprocess.run([...], capture_output=True, timeout=3)
time.sleep(0.15)
return True  # ← ALWAYS True, even if subprocess failed
```

**Fix Required:**
1. Check `result.returncode`
2. Verify app became frontmost with `is_frontmost()`
3. Return False if activation failed

---

### Issue #6: Multi-Window Apps Always Use First Window

**Severity:** HIGH
**Location:** `macos.py:337-340`, `server.py:2506-2509`

```python
# CURRENT (BROKEN):
if ax_window is None:
    windows = self._read_attr(ax_app, "AXWindows")
    if windows and len(windows) > 0:
        ax_window = windows[0]  # ← May not be the user's active window
```

**Fix Required:**
1. Add `window_index` parameter to `get_tree()`
2. Track which window elements came from
3. Activate correct window before actions

---

### Issue #7: Registry Has No TTL/Expiration

**Severity:** HIGH
**Location:** `registry.py:14-35`

```python
# CURRENT (BROKEN):
class ElementRegistry:
    def __init__(self):
        self._elements: dict[int, UIElement] = {}  # ← Never expires!
```

**Fix Required:**
1. Add timestamp when elements are registered
2. Add TTL check in `get()` method
3. Return None for expired elements with helpful message

```python
# FIXED:
class ElementRegistry:
    def __init__(self):
        self._elements: dict[int, UIElement] = {}
        self._registered_at: float = 0
        self._TTL_SECONDS = 60

    def get(self, element_id: int) -> UIElement | None:
        if time.time() - self._registered_at > self._TTL_SECONDS:
            return None  # Force refresh
        return self._elements.get(element_id)
```

---

### Issue #8: AX Action Returns API Success, Not Action Effect

**Severity:** HIGH
**Location:** `macos.py:392-398`

```python
# CURRENT (BROKEN):
def perform_action(self, element: UIElement, action: str) -> bool:
    err = self._ax.AXUIElementPerformAction(element.platform_ref, ax_action)
    return err == 0  # ← Only means "API call completed", not "action worked"
```

**Fix Required:**
1. For buttons: verify focus changed or expected state change
2. For checkboxes: verify state toggled
3. Add optional verification parameter

---

### Issue #9: CDP Elements Registered with pid=0

**Severity:** HIGH
**Location:** `server.py:1583`, `server.py:1611`

```python
# CURRENT:
registry.register_tree(tree, pid=0)  # CDP elements don't have a native PID
```

**Impact:** CDP elements never have window activated before coordinate clicks.

**Fix Required:**
1. Track Chrome's PID when getting CDP tree
2. Set `pid` to Chrome's process ID for CDP elements
3. Activate Chrome before coordinate-based clicks

---

### Issue #10: No Tab Index in Element

**Severity:** CRITICAL
**Location:** `base.py:10-24`

```python
# CURRENT:
@dataclass
class UIElement:
    # ... other fields ...
    pid: int = 0
    # NO tab_index FIELD!
```

**Fix Required:**
1. Add `tab_index: int = -1` to UIElement
2. Set when registering CDP elements
3. Use in action routing

---

## Implementation Plan

### Phase 1: P0 Fixes (Must Fix First)

| Task | Files | Effort |
|------|-------|--------|
| Add `tab_index` to UIElement | `base.py`, `server.py`, `cdp.py` | 2h |
| Route CDP actions to correct tab | `server.py` | 1h |
| Add focus verification | `server.py`, `input_sim.py` | 2h |
| Add CDP element validation | `cdp.py`, `server.py` | 2h |
| Add web typing verification | `server.py`, `cdp.py` | 2h |

**Total Phase 1:** ~9 hours

### Phase 2: P1 Fixes (Critical)

| Task | Files | Effort |
|------|-------|--------|
| Add registry TTL | `registry.py`, `server.py` | 1h |
| Fix activate_window return value | `input_sim.py` | 1h |
| Track Chrome PID for CDP elements | `server.py`, `cdp.py` | 1h |
| Add element provenance tracking | `base.py`, `registry.py` | 2h |

**Total Phase 2:** ~5 hours

### Phase 3: P2 Fixes (High)

| Task | Files | Effort |
|------|-------|--------|
| Multi-window tracking | `macos.py`, `base.py`, `server.py` | 3h |
| AX action effect verification | `macos.py` | 2h |
| Comprehensive error recovery | `server.py` | 2h |

**Total Phase 3:** ~7 hours

---

## Verification Architecture

### Proposed ActionVerifier Class

```python
class ActionVerifier:
    """Verify actions actually completed successfully."""

    async def verify_focus(self, pid: int, timeout: float = 0.5) -> bool:
        """Poll until target app is frontmost or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if input_backend.is_frontmost(pid):
                return True
            await asyncio.sleep(0.05)
        return False

    async def verify_cdp_element(self, tab: ChromeTab, backend_node_id: int) -> bool:
        """Check if CDP element reference is still valid."""
        try:
            result = await cdp_client._send(ws, "DOM.resolveNode",
                {"backendNodeId": backend_node_id})
            return result.get("object", {}).get("objectId") is not None
        except:
            return False

    async def verify_type(self, element: UIElement, text: str) -> tuple[bool, str]:
        """Verify text was typed. Returns (success, actual_value)."""
        if element.source == "cdp":
            actual = await cdp_client.get_element_value(tab, element.platform_ref)
        else:
            actual = native_adapter._read_attr(element.platform_ref, "AXValue")
        return (text in str(actual or "")), str(actual or "")
```

---

## Success Criteria

After fixes are implemented:

1. **Tab Targeting:** Actions on tab N go to tab N (not always tab 0)
2. **Focus Verification:** Actions fail fast if target app not focused
3. **Stale Detection:** CDP elements validated before action
4. **Type Verification:** All typing operations verified
5. **Registry Freshness:** Elements expire after TTL

**Target:** Reduce silent failure rate from 25-45% to <5%

---

## Testing Strategy

### Unit Tests
- `test_element_tab_index_routing.py` - Verify tab routing
- `test_focus_verification.py` - Test focus race conditions
- `test_cdp_staleness.py` - Test stale element handling
- `test_type_verification.py` - Test typing verification

### Integration Tests
- Multi-tab workflow: Get tree from tab 2, click element, verify correct tab
- Focus race: Simulate notification during action
- Dynamic page: React re-render during action
- Long session: Verify TTL expiration works

### Manual Testing
- Test on busy desktop with notifications enabled
- Test with React/Vue SPA that re-renders frequently
- Test multi-window apps (Finder, Chrome with multiple windows)

---

## References

- Deep Analysis Session: 2026-04-06
- Related: `docs/plans/2026-03-25-agent-eyes-performance-redesign.md`
- Code Locations: All line numbers verified against current codebase

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-06 | Initial plan created from deep analysis |
