# agent-eyes Deep Analysis & Brainstorm
**Date**: 2026-04-07
**Scope**: Architecture, Performance, Security, Consistency

---

## Executive Summary

The codebase has a **solid conceptual foundation** (3-tier adapter pattern, universal UIElement model, clean registry) but suffers from **one central structural debt**: `server.py` is a 2,900-line god object that owns all state, dispatch, and handlers. This single issue cascades into race conditions, untestability, duplicated tier-fallback logic, and performance bottlenecks.

**Headline numbers:**
- **19 performance bottlenecks** found (6 critical, 7 significant, 6 minor)
- **10 security findings** (3 HIGH, 5 MEDIUM, 2 LOW)
- **8 architectural issues** (2 critical, 6 important)
- Estimated **250-600ms** wasted per click/type from blocking `time.sleep()` in async handlers
- **25-45% silent failure rate** partially explained by `_cached_tabs` race condition

---

## CRITICAL ISSUES (Fix First)

### 1. `time.sleep()` in async handlers — blocks event loop 250-600ms per action
**Files**: `server.py:1332, 1475, 1495, 1522, 1974`
**Impact**: Every click/type freezes the entire MCP server
**Fix**: Replace with `await asyncio.sleep()`

### 2. Blocking `urllib.urlopen` in async CDP methods
**Files**: `cdp.py:144-166`, `cdp_persistent.py:401-411`
**Impact**: Event loop stalls up to 5 seconds on Chrome tab fetches
**Fix**: Use `asyncio.get_event_loop().run_in_executor(None, ...)` or switch to `aiohttp`

### 3. Race condition on `_cached_tabs` — no locking on shared mutable state
**File**: `server.py:1569, 1635, 1814, 2100, 2208`
**Impact**: Wrong tab closed, stale indexes, silent failures
**Fix**: Add `asyncio.Lock` or use `cdp_pool` as source of truth for Tier 2

### 4. 6 callsites bypass `ElementRegistry` API — write directly to `_elements`
**Files**: `server.py:1262, 2041, 2056, 2526, 2741, 2764`
**Impact**: Elements registered via `eyes_find`/`eyes_wait_for` immediately report as stale
**Fix**: Add `registry.register_element(el)` method, replace all direct writes

### 5. Unrestricted JS execution via `eyes_evaluate` — no audit trail
**File**: `server.py:1883-1938`
**Impact**: Prompt injection → cookie/credential exfiltration
**Fix**: Full audit logging, configurable opt-in, expression length cap

### 6. URL scheme not validated in `navigate`/`launch_browser`
**Files**: `server.py:1828, 3059`
**Impact**: `file:///etc/passwd` readable via Chrome + CDP
**Fix**: Allowlist `http`, `https`, `about`, `chrome` schemes

---

## PERFORMANCE OPTIMIZATION PLAN

### Priority 1 — Immediate (correctness + major latency)
| Issue | Current | After | Effort |
|-------|---------|-------|--------|
| `time.sleep()` → `asyncio.sleep()` | 250-600ms block | Non-blocking | 30min |
| `urllib` → `run_in_executor` | 0-5s block | Non-blocking | 1hr |
| `_next_id()` async lock → sync | Unnecessary coroutine suspend | Direct return | 15min |
| `get_event_loop()` → `get_running_loop()` | Deprecated, slower | Correct, faster | 15min |

### Priority 2 — Short-term (throughput)
| Issue | Current | After | Effort |
|-------|---------|-------|--------|
| `_enrich_subtree` serial CDP calls | 100-400ms sequential | 50-200ms parallel via `asyncio.gather` | 1hr |
| Double tree traversal in `get_tree` | 4 × O(n) walks | 1 × O(n) walk | 30min |
| Cache `get_input_backend()` singleton | Re-probed 10+ times per action | Instantiated once | 15min |
| Route `list_chrome_tabs` via `cdp_pool` | 3 HTTP calls | 0 HTTP calls | 30min |
| Defer `AXUIElementCopyActionNames` | Called for all 500+ elements | Only for interactive roles | 1hr |

### Priority 3 — Long-term (architecture)
| Issue | Current | After | Effort |
|-------|---------|-------|--------|
| Migrate handlers to Tier 2 persistent WS | N+1 WebSocket per form field | 1 shared session | 3hr |
| Dict dispatch table | 30-branch if/elif | O(1) lookup | 30min |
| `_handle_context` in executor | Blocks event loop 500ms-2s | Non-blocking | 30min |
| `_handle_wait_for` find in executor | Blocks during polling | Non-blocking | 30min |

### Expected Results
| Operation | Before | After |
|-----------|--------|-------|
| `eyes_click` / `eyes_type` | 350-700ms | 100-200ms |
| `eyes_get_web_tree` | 400-900ms | 200-450ms |
| `eyes_list_chrome_tabs` (Tier 2) | 100-500ms | <5ms |
| `eyes_fill_form` (5 fields) | 500-1500ms | 100-300ms |

---

## ARCHITECTURAL IMPROVEMENTS

### A. Break up the God Object (`server.py`)
**Current**: 2,900 lines, 6 singletons, 30+ handlers, 780-line TOOLS schema
**Target structure**:
```
server.py          → slim entry point (MCP app, startup, dispatch table)
server_context.py  → ServerContext dataclass (replaces module-level globals)
tools.py           → TOOLS schema list
handlers/
  native.py        → get_tree, find, element_at, get_subtree, get_focused
  browser.py       → get_web_tree, navigate, evaluate, new_tab, close_tab, list_tabs
  input.py         → click, type, press_key, scroll, drag, fill_form
  lifecycle.py     → status, context, setup, app, window, launch_browser
  shadow.py        → shadow mode handlers
```
**Unlocks**: Unit testing, dependency injection, isolated development

### B. Consolidate CDP Files
**Current**: `cdp.py` (1139 lines) + `cdp_persistent.py` (412 lines) with duplicate `ChromeTab`, port discovery, and cross-references
**Target**:
```
cdp_types.py       → ChromeTab, _build_tree, _simplify_color, _ENRICH_ROLES
cdp_legacy.py      → per-request WebSocket client (Tier 3 fallback)
cdp_persistent.py  → single persistent WebSocket (Tier 2 primary)
```

### C. Centralize Tier Routing
**Current**: `TierManager` exists but 8+ handlers implement their own fallback chains
**Target**: `TierRouter.get_session(tab_index)` returns the best available session
**Benefit**: Consistent fallback behavior, single place to add Tier 1 (extension)

### D. Guard `ExtensionBridge.send()` Against MCP Process
**Risk**: `send()` writes to `sys.stdout.buffer` which IS the MCP transport
**Fix**: `raise RuntimeError` if called from server process (not native host)

### E. Add `ServerContext` for Testability
```python
@dataclass
class ServerContext:
    registry: ElementRegistry
    native_adapter: BaseAdapter | None
    cdp_client: CDPClient
    cdp_pool: PersistentCDP
    tier_manager: TierManager
    ext_bridge: ExtensionBridge
    input_backend: InputBackend
```

---

## SECURITY HARDENING

### Priority Order
1. **URL scheme validation** on `navigate` + `launch_browser` + Chrome flag injection guard
2. **AppleScript `idx` integer validation** before string interpolation (close-tab fallback)
3. **`eyes_evaluate` audit logging** — full expression, not truncated 200 chars
4. **`eyes_file_upload` path blocklist** — reject `.ssh`, `.aws`, `.gnupg`, `/etc`
5. **`eyes_wait_for` timeout cap** — max 60 seconds
6. **Generic error messages** in `call_tool` — hide internal paths from caller
7. **Log `_auto_install_platform_deps`** — don't silently install packages
8. **Validate CDP WebSocket URL** — ensure it points to localhost
9. **ReDoS protection** — cap regex pattern length in `eyes_find`

---

## CONSISTENCY ISSUES

| Area | Inconsistency | Fix |
|------|--------------|-----|
| Tier fallback | 8 handlers have different fallback chains | Centralize in `TierRouter` |
| Registry writes | 6 callsites bypass API, others use it correctly | All go through `register_element()` |
| `match_type` in `_handle_find` | `role` always uses contains, `name`/`value` respect `match_type` | Apply `_match_text` to all three fields |
| `max_depth` caps | `get_tree`: 20, `get_web_tree`: 10, `get_subtree`: 15 | Document or standardize |
| Error message format | Some return `"ERROR: ..."`, some `f"Error: {e}"` | Standardize to `"ERROR: {msg}"` |
| Platform guards | Some handlers have `sys.platform` checks, some have bare imports | Consistent `try/except ImportError` pattern |

---

## WHAT'S DONE WELL

- **Clean adapter abstraction** — `BaseAdapter` ABC with proper contract
- **Universal `UIElement` model** — native + CDP elements in one structure
- **`ElementRegistry` design** — TTL, search, and tuple-return API are all correct
- **`CDPConnection` (persistent)** — single read loop, sessionId dispatch, auto-attach — production-grade
- **Graceful degradation** — Tier 3 → AppleScript → CLI fallback chain is thoughtful
- **`platform_utils.py`** — clean, stateless utility layer
- **Extension Manifest V3** — proper security-restricted model
- **Focus verification before keyboard injection** — prevents race conditions

---

## RECOMMENDED SPRINT ORDER

**Sprint 1 (P0 — Reliability + Safety):**
1. Replace `time.sleep()` with `await asyncio.sleep()` in all async handlers
2. Wrap `urllib.urlopen` in `run_in_executor`
3. Add `asyncio.Lock` to `_cached_tabs` mutations
4. Add `registry.register_element()` and fix 6 callsites
5. URL scheme validation on `navigate` + `launch_browser`
6. AppleScript `idx` integer validation

**Sprint 2 (Performance):**
1. `asyncio.gather` for `_enrich_subtree` box+visual calls
2. Merge `_tree_has_web_content` + `_count_interactive` into single pass
3. Cache `get_input_backend()` as singleton
4. Route `list_chrome_tabs` through `cdp_pool`
5. Defer `AXUIElementCopyActionNames` to interactive roles only

**Sprint 3 (Architecture):**
1. Extract `ServerContext` dataclass
2. Move `TOOLS` to `tools.py`
3. Extract handlers into domain-grouped modules
4. Consolidate CDP types into `cdp_types.py`
5. Centralize tier routing in `TierRouter`
6. Guard `ExtensionBridge.send()`

**Sprint 4 (Security Hardening):**
1. `eyes_evaluate` audit logging + opt-in config
2. `eyes_file_upload` path blocklist
3. `eyes_wait_for` timeout cap
4. Generic error messages in `call_tool`
5. CDP WebSocket URL origin validation
6. ReDoS protection in `eyes_find`
