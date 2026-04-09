# agent-eyes Corrected Deep Audit — 2026-04-08

> Re-verified every claim from the initial audit against actual source code.
> This report documents what was **correct**, what was **wrong/overstated**,
> what was **missed entirely**, and the final corrected priority list.

---

## CORRECTIONS: What the Initial Audit Got Wrong

### WRONG-1: BUG-3 "N × 15s cascading hangs" — OVERSTATED
**Claimed:** Orphaned futures in CDPConnection cause `N × 15s` sequential hangs.
**Actual:** `CDPSession.send()` (L72) already uses `asyncio.wait_for(future, timeout=15.0)`. All pending futures timeout **concurrently** (asyncio is cooperative). Max hang = **15s total**, not N × 15s.
**Real issue:** `disconnect()` (L198) calls `self._sessions.clear()` which drops references to CDPSession objects but doesn't cancel their pending futures. Those futures just timeout naturally. Wasteful but not catastrophic.
**Corrected severity:** LOW (was HIGH)

### WRONG-2: ExtensionBridge futures "never timeout" — NOT ACTIVELY USED
**Claimed:** Pending Futures in ExtensionBridge hang indefinitely.
**Actual:** `ExtensionBridge.send()` (L154-155) is explicitly documented as "provided for future use" — the current MVP only checks `is_connected`. No code path actually calls `send()` today. The futures issue is **theoretical**.
**Corrected severity:** INFO/FUTURE (was HIGH)

### WRONG-3: P6 "180 serial CDP round-trips" — MATH ERROR
**Claimed:** 180 round-trips in `_enrich_tree`.
**Actual:** `_enrich_subtree` (L246) runs `_get_box_model` (1 call) and `_get_visual_summary` (2 calls) via `asyncio.gather`. Effective serial path per element = max(1, 2) = **2 round-trips**. For 60 elements: **~120 round-trips**, not 180.

### WRONG-4: P9 "dead wrappers called a third time" — WRONG
**Claimed:** `_tree_has_web_content` and `_count_interactive` are "dead wrappers" called in the hot path.
**Actual:**
- `_tree_has_web_content` has **ZERO callers** anywhere in the codebase — truly dead code
- `_count_interactive` is called only at **L2659** (`_handle_get_subtree`), NOT in `_handle_get_tree`
- Neither is called in the `_handle_get_tree` hot path. The audit was wrong about them adding overhead there.

### WRONG-5: P5 "_get_chrome_pid spawns 2-3 subprocesses" — UNDERCOUNTED
**Claimed:** 2-3 subprocesses
**Actual:** Up to **4**: 3x `pgrep -x` (L1694-1701) for "Google Chrome", "Chromium", "Chrome" + 1x `pgrep -f` fallback (L1703-1704). Minor inaccuracy.

### WRONG-6: S1 "AppleScript injection via close-tab" — OVERSTATED
**Claimed:** HIGH severity AppleScript injection.
**Actual:** `idx` is validated as `isinstance(idx, int)` at L2186 AND cast via `int(idx) + 1` in the f-string (L2190). Integer-only interpolation into AppleScript is safe. No string user input reaches the script.
**Corrected severity:** INFO (was HIGH)

### WRONG-7: S8 "User URL as Chrome subprocess argument" — REDUNDANT
**Claimed:** MEDIUM severity argument injection in `_handle_launch_browser`.
**Actual:** URL is already validated by `_validate_url()` at L3103-3105 (blocks `--` prefixes and non-allowlisted schemes). Chrome handles URL arguments safely. This finding is entirely dependent on S4 (schemeless bypass). Once S4 is fixed, S8 is a non-issue.
**Corrected severity:** N/A (duplicate of S4)

### WRONG-8: P11 "_simplify_color rebuilds _COLORS list" — TRIVIALLY LOW
**Claimed:** "60 list allocations per `eyes_get_web_tree` call".
**Actual:** Technically correct but massively overstated. The `_COLORS` list is 8 entries of small tuples. Python list literal allocation is ~200ns. 60 calls = ~12μs — **0.01ms total**. Dwarfed by the CDP round-trips (each ~1-5ms). This is noise, not a bottleneck.
**Corrected severity:** NEGLIGIBLE (was MINOR)

---

## MISSED: What the Initial Audit Failed to Find

### MISSED-1: `_enrich_subtree` dead-code limit check (ACTUAL BUG)
**File:** `cdp.py:239-241` | **Severity:** LOW (correctness)

```python
async def _enrich_subtree(self, ws, element: UIElement, limit: int) -> int:
    enriched = 0
    if enriched >= limit:  # ALWAYS False — enriched was just set to 0!
        return enriched
```

`enriched` is initialized to 0, then immediately checked against `limit` (60). This is dead code — the condition is never true. The limit still works via the recursive call at L262 (`limit - enriched`), so functionally it's fine. But it's a copy-paste bug that suggests the original intent was to receive `enriched` as a cumulative parameter.

### MISSED-2: `_handle_type` and `_handle_click` NOT migrated to Tier 2 (SIGNIFICANT)
**File:** `server.py:1411-1421` | **Severity:** HIGH (performance)

`_handle_type` routes CDP elements directly to `cdp_client.type_text()` (Tier 3 — per-operation WebSocket). It does NOT use `_get_cdp_session()` to try Tier 2 first.

Compare:
- `_handle_evaluate` → uses `_get_cdp_session()` → Tier 2 first ✓
- `_handle_navigate` → uses `_get_cdp_session()` → Tier 2 first ✓
- **`_handle_type` → uses `cdp_client.type_text()` → Tier 3 ONLY** ✗

This means **typing always opens a new WebSocket per call**, even when a persistent Tier 2 connection is available. Combined with the per-char `Input.dispatchKeyEvent` (P4), typing 50 chars = 1 new WebSocket + 100 CDP round-trips, when Tier 2 could do it over the existing connection.

**Impact:** This is the single biggest performance issue for typing operations.

### MISSED-3: `_tree_has_web_content` is completely dead code
**File:** `server.py:1212-1214` | **Severity:** LOW (cleanup)

```python
def _tree_has_web_content(element) -> bool:
    return _analyze_tree(element)[0]
```

Zero callers in the entire codebase. Should be deleted.

### MISSED-4: `_get_chrome_pid` false-positive with `pgrep -f`
**File:** `server.py:1703-1704` | **Severity:** LOW (correctness)

```python
result = subprocess.run(
    ["pgrep", "-f", "Google Chrome"],
    capture_output=True, text=True, timeout=2,
)
```

`pgrep -f` matches the full command line of ALL processes. If the agent-eyes Python process was launched with "Google Chrome" as an argument string (e.g., in a test), `pgrep -f` would return the Python process's PID instead of Chrome's. Edge case but could cause confusion.

### MISSED-5: `_handle_type` Tier 3 path has no `_get_cdp_session` — stale tab reference
**File:** `server.py:1412-1421` | **Severity:** MEDIUM (correctness)

After the element source check, `_handle_type` reads `_cached_tabs[tab_idx]` directly. If Tier 2 is connected and tabs have changed (via `Target.attachedToTarget`), the `_cached_tabs` list may be stale. The Tier 2 `cdp_pool` has live tab tracking but `_handle_type` doesn't use it.

### MISSED-6: No cleanup for CDPSession `_pending` when session is detached
**File:** `cdp_persistent.py:372-373` | **Severity:** MEDIUM

When `_on_detached` fires (tab closed), the session is removed from `_sessions` dict (L373) but its `_pending` futures are not cancelled. If a command was in-flight to a tab that was closed, the caller hangs until the 15s timeout.

**Fix:** Cancel pending futures in `_on_detached`:
```python
def _on_detached(self, params: dict) -> None:
    session_id = params.get("sessionId", "")
    session = self._sessions.pop(session_id, None)
    if session:
        err = RuntimeError("Tab was closed")
        for fut in session._pending.values():
            if not fut.done():
                fut.set_exception(err)
        session._pending.clear()
    # ... rest of cleanup
```

### MISSED-7: `_handle_get_web_tree` may be sync — blocking native_adapter on asyncio thread
**File:** `server.py:1145` | **Severity:** CONFIRMED from original audit (BUG-4)

The initial audit correctly identified this but I want to confirm: `_handle_get_tree` calls `native_adapter.get_tree()` synchronously. Since the dispatch system supports both sync and async handlers, this blocking call on the asyncio thread IS the problem. The fix (`run_in_executor`) requires making the handler async first.

---

## CONFIRMED: What the Initial Audit Got Right

| ID | Finding | Verified | Notes |
|---|---------|----------|-------|
| BUG-1 | `_send` infinite loop, no timeout | ✅ CONFIRMED | L1030: `while True: await ws.recv()` — no timeout, no deadline |
| BUG-4 | Blocking sync on asyncio thread | ✅ CONFIRMED | `find_elements` (L2069) and `get_tree` (L1145) block event loop |
| BUG-5 | `_validate_url` schemeless bypass | ✅ CONFIRMED | L1848: `if parsed.scheme and ...` — empty scheme passes |
| P2 | Per-operation WebSocket | ✅ CONFIRMED | 17 methods each open/close their own WebSocket |
| P4 | Per-char `dispatchKeyEvent` | ✅ CONFIRMED | L508-520: 2N CDP calls for N chars |
| P5 | `_get_chrome_pid` subprocesses | ✅ CONFIRMED | Up to 4 (not 2-3) `pgrep` calls, uncached |
| P7 | `_ensure_tabs` new aiohttp session | ✅ CONFIRMED | L117: `aiohttp.ClientSession()` created per check |
| P8 | Linux click = 2 subprocesses | ✅ CONFIRMED | L595-601: separate `mousemove` + `click` |
| P10 | `registry.find()` re-lowercases | ✅ CONFIRMED | L92-96: `.lower()` on every element per call |
| P14 | `paste_text` 150ms sleep | ✅ CONFIRMED | L360: 50ms + L362: 100ms on asyncio thread |
| S2 | Unrestricted JS via `eyes_evaluate` | ✅ CONFIRMED | L1926-1928: expression → `Runtime.evaluate` |
| S3 | Runtime pip install from PATH | ✅ CONFIRMED | L87-99: `shutil.which("uv")` + `subprocess.check_call` |
| S5 | File upload blocklist incomplete | ✅ CONFIRMED | L2285-2300: no symlink resolve, limited blocklist |
| S6 | CDPClient missing WS hostname check | ✅ CONFIRMED | L159-166: `ws_url` used without hostname validation |
| S7 | Linux input `--` flag injection | ✅ CONFIRMED | L472, 477: no `--` separator before text |
| S9 | JS expression logged at INFO | ✅ CONFIRMED | L576-578: `logger.info(...)` with expression |
| S11 | ReDoS via regex match | ✅ CONFIRMED | L1234-1237: unbound `re.search` with user regex |

---

## CORRECTED PRIORITY LIST

### Phase 0 — Actual Bugs (fix NOW)

| # | Issue | File:Line | Why |
|---|-------|-----------|-----|
| 1 | **`_send` infinite loop** — no timeout | `cdp.py:1030` | Hung Chrome = frozen MCP server forever. Every Tier 3 operation exposed. |
| 2 | **`_validate_url` schemeless bypass** | `server.py:1848` | `//evil.com` passes validation. One-line fix. |
| 3 | **Session futures not cancelled on tab close** | `cdp_persistent.py:372` | In-flight commands to closed tabs hang 15s. |
| 4 | **`--` missing from xdotool/ydotool/wtype** | `input_sim.py:472,477` | Text starting with `--` interpreted as flags. |
| 5 | **CDPClient.list_tabs missing WS hostname check** | `cdp.py:159` | DNS rebinding could redirect CDP connections. |

### Phase 1 — High-Impact Performance

| # | Issue | File:Line | Impact |
|---|-------|-----------|--------|
| 6 | **`_handle_type` not using Tier 2** (NEW) | `server.py:1411` | Typing always opens new WS + per-char dispatch. Single biggest perf issue. |
| 7 | **`run_in_executor` for `find_elements`/`get_tree`** | `server.py:2069,1145` | Unblocks asyncio event loop during 100-500ms native calls. |
| 8 | **`Input.insertText` instead of per-char** | `cdp.py:508` | 2N CDP calls → 1 call for typing. |
| 9 | **Cache Chrome PID** | `server.py:1685` | 4 subprocess spawns → 0 per `get_web_tree`. |
| 10 | **Combine xdotool `mousemove + click`** | `input_sim.py:595` | 2 subprocesses → 1 per click. |

### Phase 2 — Medium-Impact Improvements

| # | Issue | File:Line | Impact |
|---|-------|-----------|--------|
| 11 | File upload: allowlist + `realpath` | `server.py:2285` | Symlink bypass + incomplete blocklist. |
| 12 | `_discover_port` sync socket on asyncio | `cdp_persistent.py:394` | Up to 1s block at startup. |
| 13 | `paste_text` 150ms sync sleep | `input_sim.py:360` | Blocks event loop. |
| 14 | `registry.find()` pre-lowercase queries | `registry.py:92` | Avoid re-lowercasing per element. |
| 15 | Require scheme in `_validate_url` | `server.py:1848` | Defense-in-depth for URL handling. |

### Phase 3 — Cleanup & Architecture

| # | Issue | Notes |
|---|-------|-------|
| 16 | Migrate ALL handlers to `_get_cdp_session()` | `_handle_type`, `_handle_click` still use Tier 3 directly. |
| 17 | Delete dead `_tree_has_web_content` | Zero callers. |
| 18 | Fix `_enrich_subtree` dead limit check | L239-241 dead code. |
| 19 | Downgrade `evaluate` log to DEBUG | L576: leaks expressions to INFO logs. |
| 20 | Batch `DOM.describeNode` calls in `_enrich_tree` | ~120 serial round-trips → ~60 with batching. |
| 21 | Clean up `_pending` futures in `disconnect()` | Wasteful 15s timeouts on disconnect. |

### Phase 4 — Cross-Platform (unchanged from original)

| # | Action |
|---|--------|
| 22 | Replace xdotool/ydotool with python-evdev on Linux |
| 23 | Monitor PyScreenReader, Acacia, libei quarterly |

---

## SUMMARY OF CHANGES FROM INITIAL AUDIT

| Category | Initial Count | Corrected | Change |
|----------|--------------|-----------|--------|
| **Bugs actually requiring fix** | 6 | 5 | Removed ExtensionBridge (not in use) |
| **HIGH severity findings** | 5 | 3 | Downgraded S1, ExtensionBridge |
| **Performance findings** | 15 | 12 | Removed 3 negligible/wrong items, added 1 NEW major |
| **Security findings** | 12 | 9 | Removed 3 overstated/duplicate, kept 9 verified |
| **Missed findings** | 0 | 7 | 7 NEW issues found on re-analysis |

### Single Most Important Finding (NEW)
**`_handle_type` bypasses Tier 2 entirely.** This means every typing operation opens a new WebSocket and sends per-character CDP events, even when a persistent connection is available. Fixing this (#6 + #8) would make typing ~100x faster (1 persistent call vs N×2 new-connection calls).
