# agent-eyes Deep Codebase Audit — 2026-04-08

## Executive Summary

Full audit across 4 dimensions: **architecture**, **performance**, **security**, **cross-platform alternatives**. Found **15 performance bottlenecks**, **12 security findings**, and **1 recommended dependency replacement**.

---

## PART 1: CRITICAL BUGS & ISSUES

### BUG-1: `CDPClient._send` infinite loop (no timeout)
**File:** `cdp.py:1030-1037` | **Severity:** CRITICAL

```python
while True:
    response = json.loads(await ws.recv())
    if response.get("id") == msg_id:
        return response.get("result", {})
```

No timeout. Hung Chrome = frozen MCP server forever. Every CDP tool call is exposed.

**Fix:** Add `asyncio.wait_for` with 15s deadline.

---

### BUG-2: ExtensionBridge Futures never timeout
**File:** `extension_bridge.py` | **Severity:** HIGH

Pending Futures in `_pending` dict never resolve if extension crashes → resource leak + infinite hang.

**Fix:** `asyncio.wait_for(fut, timeout=30)` on all bridge calls.

---

### BUG-3: CDPConnection orphaned futures on disconnect
**File:** `cdp_persistent.py:54, 186-201` | **Severity:** HIGH

When `_read_loop` exits, all pending futures across sessions remain unresolved → up to `N × 15s` hangs.

**Fix:** Cancel all pending futures in `_read_loop`'s `finally` block.

---

### BUG-4: Blocking sync calls on asyncio event loop
**File:** `server.py:2067-2078, 1145-1156` | **Severity:** HIGH

`native_adapter.find_elements()` and `native_adapter.get_tree()` are synchronous macOS AX calls (100-500ms each) running on the asyncio thread. Blocks ALL other coroutines.

**Fix:** `await loop.run_in_executor(None, native_adapter.find_elements, ...)` for all native adapter calls.

---

### BUG-5: `_validate_url` schemeless bypass
**File:** `server.py:1842-1850` | **Severity:** MEDIUM

URL `//evil.com/path` has empty scheme → passes validation → Chrome interprets as protocol-relative.

**Fix:** Require non-empty scheme: `if not parsed.scheme: return "ERROR: ..."`

---

### BUG-6: No auto-reconnect for CDPConnection
**File:** `cdp_persistent.py` | **Severity:** MEDIUM

Once WebSocket drops, no automatic recovery. Manual reconnect required.

---

## PART 2: PERFORMANCE BOTTLENECKS (Priority Order)

| # | Issue | Impact | File |
|---|-------|--------|------|
| P1 | `_send` infinite loop (no timeout) | Server freeze | `cdp.py:1030` |
| P2 | Per-operation WebSocket reconnect (17 methods) | ~30ms/action overhead | `cdp.py` (all actions) |
| P3 | Blocking `find_elements` on asyncio thread | Event loop blocked 100-500ms | `server.py:2067` |
| P4 | `type_text` N serial CDP calls per char | 2N round-trips per input | `cdp.py:508-520` |
| P5 | `_get_chrome_pid` spawns 2-3 subprocesses per call | ~30ms per `get_web_tree` | `server.py:1685` |
| P6 | `_enrich_tree` 180 serial CDP round-trips | Dominates `get_web_tree` latency | `cdp.py:237-296` |
| P7 | `_ensure_tabs` creates new aiohttp session per check | Unnecessary alloc + 2s timeout | `server.py:1821` |
| P8 | Linux click = 2 subprocess spawns | 10ms overhead per click | `input_sim.py:594` |
| P9 | `_analyze_tree` called 2x on same tree | Redundant O(n) walks | `server.py:1151` |
| P10 | `registry.find()` re-lowercases all elements per call | O(n) string alloc per search | `registry.py:87` |
| P11 | `_simplify_color` rebuilds color list per call | 60 allocs per tree enrichment | `cdp.py:348` |
| P12 | Blocking `get_tree` on asyncio thread | Event loop blocked 50-500ms | `server.py:1145` |
| P13 | `_discover_port` sync socket on asyncio thread | Up to 1s block at startup | `cdp_persistent.py:386` |
| P14 | `paste_text` 150ms fixed sleep on asyncio thread | Blocks event loop 150ms | `input_sim.py:360` |
| P15 | `_pending` dict accumulates orphans | N × 15s hangs on reconnect | `cdp_persistent.py:54` |

### Quick Wins

1. **`Input.insertText`** instead of per-char `Input.dispatchKeyEvent` → 2N calls → 1 call
2. **Cache Chrome PID** with staleness guard → 3 subprocesses → 0
3. **Combine xdotool** `mousemove + click` into single command → 2 spawns → 1
4. **Move `_COLORS`** to class-level constant → 60 allocs → 0

---

## PART 3: SECURITY FINDINGS

| # | Finding | Severity | File |
|---|---------|----------|------|
| S1 | AppleScript injection via tab close | HIGH | `server.py:2190` |
| S2 | `eyes_evaluate` unrestricted JS execution | HIGH | `server.py:1912` |
| S3 | Runtime pip install from PATH | MEDIUM | `server.py:84` |
| S4 | Schemeless URL bypass | MEDIUM | `server.py:1842` |
| S5 | File upload blocklist incomplete + symlink bypass | MEDIUM | `server.py:2285` |
| S6 | CDPClient missing WebSocket hostname validation | MEDIUM | `cdp.py:155` |
| S7 | Linux input `--` flag injection | MEDIUM | `input_sim.py:470` |
| S8 | User URL as Chrome subprocess arg | MEDIUM | `server.py:3160` |
| S9 | JS expression logged at INFO | LOW | `cdp.py:576` |
| S10 | `activate_window` AppleScript pid injection | LOW | `input_sim.py:403` |
| S11 | ReDoS via `eyes_find` regex match | LOW | `server.py:1234` |
| S12 | Native messaging manifest ephemeral path | INFO | `server.py:3046` |

### Critical Security Fixes

1. **Add `--` end-of-options** to xdotool/ydotool/wtype calls
2. **Validate WebSocket hostname** in `CDPClient.list_tabs` (match `cdp_persistent.py` behavior)
3. **Require scheme** in `_validate_url`
4. **Replace file upload blocklist with allowlist** + `os.path.realpath` for symlinks
5. **Downgrade** `eyes_evaluate` log from INFO to DEBUG

---

## PART 4: CROSS-PLATFORM ALTERNATIVES

### Current Pain Points
- **macOS**: PyObjC (5 packages) — heavy, slow install, uvx dependency loop
- **Linux input**: xdotool (X11) vs ydotool (Wayland) — split, external binaries
- **Linux a11y**: AT-SPI via GI — system package, not pip-installable

### Recommended Replacement

| Current | Replace With | Benefit |
|---------|-------------|---------|
| xdotool + ydotool (Linux input) | **python-evdev + uinput** | Kernel-level, works X11+Wayland, no external binaries, pip-installable |

### Why python-evdev

- Works on **both X11 and Wayland** (kernel-level, display-server agnostic)
- **pip install evdev** — no external binary dependencies
- **Low latency** — direct kernel uinput device
- **Actively maintained** — regular releases
- Eliminates the xdotool/ydotool split entirely
- Requirement: udev rule for non-root access (`/dev/uinput` permissions)

### NOT Recommended

| Library | Why Not |
|---------|---------|
| PyAutoGUI | Abandoned, screenshot-based |
| pynput | Wayland broken, X11 only |
| Playwright | Manages own browser, not connect-to-existing |
| Selenium | Heavier than CDP, no tree API |
| AccessKit | Provider (exposes trees), not consumer (reads trees) |

### Watch List (Future)

| Library | Status | Potential |
|---------|--------|-----------|
| **PyScreenReader** | Alpha | Could unify macOS + Linux adapters into one |
| **Acacia (Igalia)** | WIP | Backed by Chromium a11y team |
| **libei (hzy)** | Early | "Correct" Wayland solution, compositor-integrated |

### Architecture Verdict

The current `BaseAdapter` pattern with platform-specific implementations is **correct**. No production-ready unified cross-platform accessibility tree library exists today. The adapter pattern properly isolates platform complexity. Best improvements are incremental.

---

## PART 5: ERROR HANDLING AUDIT

| File | `except Exception` count | Risk |
|------|------------------------|------|
| applescript.py | 11 | HIGH — swallows CancelledError |
| cdp.py | 25 | HIGH — masks connection failures |
| input_sim.py | 26 | HIGH — hides input failures |
| server.py | 18 | MEDIUM |
| cdp_persistent.py | 4 | MEDIUM |

**Recommendation:** Replace `except Exception` with specific exceptions. At minimum, re-raise `asyncio.CancelledError` and `KeyboardInterrupt` in all async code paths.

---

## PART 6: IMPLEMENTATION PRIORITY

### Phase 1 — Correctness (blocks everything)
- [ ] Add timeout to `CDPClient._send` while loop
- [ ] Cancel orphaned futures in `CDPConnection._read_loop` finally
- [ ] Fix `_validate_url` schemeless bypass
- [ ] Add `--` to xdotool/ydotool/wtype calls
- [ ] Validate WS hostname in `CDPClient.list_tabs`

### Phase 2 — Performance (high impact)
- [ ] `run_in_executor` for all native adapter calls
- [ ] `Input.insertText` instead of per-char dispatch
- [ ] Cache Chrome PID
- [ ] Short-circuit `_ensure_tabs` via `cdp_pool.is_connected`
- [ ] Combine xdotool mousemove+click

### Phase 3 — Architecture (long-term)
- [ ] Migrate CDPClient actions to CDPSession (single WebSocket)
- [ ] Batch `DOM.describeNode` calls in `_enrich_tree`
- [ ] Replace xdotool/ydotool with python-evdev on Linux
- [ ] File upload allowlist instead of blocklist
- [ ] Pre-lowercase strings in ElementRegistry

### Phase 4 — Monitoring
- [ ] Add `time.perf_counter()` logging around `_dispatch()`
- [ ] Track CDP timeout frequency
- [ ] Quarterly check: PyScreenReader, Acacia, libei maturity
