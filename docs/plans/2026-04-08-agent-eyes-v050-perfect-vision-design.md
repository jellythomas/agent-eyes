# agent-eyes v0.5.0 — "Perfect Vision" Design

**Date:** 2026-04-08
**Author:** Jelly Thomas + Claude
**Status:** Approved

## Core Philosophy

agent-eyes is the structured-text alternative to screenshots. It acts as eyes for AI agents so they don't need to screenshot and read from images — that's slow, expensive, and eats context.

### Design Principles

- Every tool response < 4KB unless explicitly requested otherwise
- Every action completes in < 500ms (network-bound exceptions: navigate, wait)
- Zero silent failures — every error is explicit and actionable
- One way to do each thing — no redundant tools
- No images, ever — structured text beats pixels, always
- Default to minimal, opt-in to verbose

## Section 1: What We Kill

| Feature | Why |
|---------|-----|
| Chrome Extension (Tier 1) | Built but never wired for live dispatch. CDP does everything. 500+ lines of dead code. |
| `eyes_element_at` | Coordinate-based lookup. AI agents think in names/roles, not pixels. |
| `eyes_get_ocr_hints` | OCR contradicts "no images" philosophy. Read text from accessibility tree. |
| `eyes_setup` / `eyes_setup_apply` | Replaced by `/agent-eyes:install` + `/agent-eyes:init` skill flow. |
| `eyes_launch_browser` | Agent shouldn't launch browsers — connect to what's running. |

**Result:** ~32 tools → ~27 tools. Less surface area, fewer bugs, clearer API.

## Section 2: Tool Redesign (Breaking Changes)

Drop the `eyes_` prefix — MCP server namespace already scopes these. Shorter = fewer tokens.

### Tier 1 — Observe (read state, zero side effects)

| Old | New | Change |
|-----|-----|--------|
| `eyes_context` | `context` | Rename |
| `eyes_status` | `status` | Rename |
| `eyes_get_web_tree` | `web_tree` | Rename, add `interactive_only` param (default: true) |
| `eyes_get_tree` | `tree` | Rename, add `interactive_only` param (default: true) |
| `eyes_get_subtree` | `subtree` | Rename |
| `eyes_get_focused` | `focused` | Rename |
| `eyes_find` | `find` | Rename |
| `eyes_list_apps` | `list_apps` | Rename |
| `eyes_list_chrome_tabs` | `list_tabs` | Rename, drop "chrome" |

### Tier 2 — Act (side effects)

| Old | New | Change |
|-----|-----|--------|
| `eyes_click` | `click` | Rename |
| `eyes_type` | `type` | Rename |
| `eyes_press_key` | `press_key` | Rename |
| `eyes_hover` | `hover` | Rename |
| `eyes_scroll` | `scroll` | Rename |
| `eyes_drag` | `drag` | Rename, mark advanced |
| `eyes_fill_form` | `fill_form` | Rename |
| `eyes_file_upload` | `upload` | Rename |
| `eyes_handle_dialog` | `dialog` | Rename |
| `eyes_evaluate` | `js` | Rename (it runs JavaScript) |
| `eyes_wait_for` | `wait` | Rename |

### Tier 3 — Navigate (change what you're looking at)

| Old | New | Change |
|-----|-----|--------|
| `eyes_navigate` | `navigate` | Rename |
| `eyes_new_tab` | `new_tab` | Rename |
| `eyes_close_tab` | `close_tab` | Rename |
| `eyes_window` | `window` | Rename |
| `eyes_app` | `app` | Rename |
| `eyes_shadow` | `shadow` | Rename |

### New Tools

| Tool | Purpose |
|------|---------|
| `pierce` | Shadow DOM content via CDP `DOM.getFlattenedDocument(pierce: true)` |
| `install_check` | Returns install state — used internally by `:init` |

**Final count: 27 tools**

### The `interactive_only` Parameter

When `web_tree(interactive_only=true)` (the default):
- Only return elements with roles: button, link, textbox, checkbox, radio, select, tab, menuitem, slider, switch
- Skip static text, decorative elements, layout containers
- Typical page: 2000+ elements → 50-100 interactive elements
- **80% token reduction**

## Section 3: Architecture — Simplified Tiers

Kill Tier 1 (Extension). Simplify to 2 tiers:

```
Tier A: CDP (browser)          Tier B: Native (desktop/OS)
├─ Persistent WebSocket        ├─ macOS: AXUIElement
├─ Session multiplexing        ├─ Linux: AT-SPI2
├─ Shadow DOM piercing         ├─ Windows: UI Automation
├─ JS injection bridge         └─ Input: platform-native
└─ Input: system-level
```

### Dispatch Logic

```
user calls a tool →
  Is target a browser tab?
    YES → Tier A (CDP)
    NO  → Tier B (Native)
  CDP not available?
    → Tier B fallback (native accessibility can read Chrome too)
```

### Shadow DOM Piercing Pipeline

```
1. CDP: DOM.getFlattenedDocument(pierce=true)  → full DOM including shadow roots
2. CDP: Accessibility.getFullAXTree()          → accessibility properties
3. Merge: map DOM nodes to AX nodes           → unified tree
4. Filter: if interactive_only, prune          → minimal token footprint
5. Register: assign element IDs               → for click/type targeting
```

## Section 4: Installation, Setup & Plugin Architecture

agent-eyes becomes a proper Claude Code plugin for reliable slash commands.

### Plugin Structure

```
agent-eyes repo (or marketplace install):
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json
├── skills/
│   ├── agent-eyes/SKILL.md      ← auto-trigger on browser/UI keywords
│   ├── install/SKILL.md          ← /agent-eyes:install
│   └── init/SKILL.md             ← /agent-eyes:init
├── src/agent_eyes/               ← MCP server code
└── pyproject.toml
```

### plugin.json

```json
{
  "name": "agent-eyes",
  "version": "0.5.0",
  "description": "Accessibility-tree vision for AI agents — see and interact with any application without screenshots",
  "author": {
    "name": "Jelly Thomas",
    "url": "https://github.com/jellythomas"
  },
  "homepage": "https://github.com/jellythomas/agent-eyes",
  "license": "MIT",
  "keywords": ["mcp", "accessibility", "browser", "desktop", "automation"],
  "mcpServers": {
    "agent-eyes": {
      "command": "uvx",
      "args": ["agent-eyes"]
    }
  },
  "skills": "./skills/"
}
```

### /agent-eyes:install Flow

```
1. Detect platform → "macOS 15.4 (Apple Silicon)"
2. Check deps → list installed/missing
3. Check system permissions → accessibility, CDP
4. Install platform extras → pip install agent-eyes[macos|linux|windows]
5. Grant permissions (guided) → OS-specific instructions
6. Self-test → read one native element, connect CDP
7. Save state → ~/.agent-eyes/install.json
```

### /agent-eyes:init Flow

```
1. Check install state → read ~/.agent-eyes/install.json
   ├─ Missing/outdated → auto-trigger /agent-eyes:install
   └─ OK → continue
2. Scan AI tool configs → Claude Code, Cursor, VS Code, etc.
3. Detect competing servers → Playwright MCP, Puppeteer, etc.
4. Offer replacement → "Replace with agent-eyes? [Y/n]"
5. Write MCP configs → done
```

### Install State File (~/.agent-eyes/install.json)

```json
{
  "installed": true,
  "platform": "darwin",
  "arch": "arm64",
  "deps_installed": ["pyobjc-framework-ApplicationServices", "..."],
  "permissions": {"accessibility": true, "cdp": true},
  "version": "0.5.0",
  "installed_at": "2026-04-08T..."
}
```

## Section 5: Token Efficiency & Response Format

### Default Response Formats

**web_tree (interactive_only=true, the default):**
```
[1] link "About"
[2] link "Store"
[3] textbox "Search" focused
[4] button "Google Search"
```

**Action confirmations (click, type, press_key):**
```
✓ clicked [3] textbox "Search"
```

**Errors:**
```
✗ click [7]: element not found (stale registry)
  → try: web_tree to refresh, then click by text
```

**context:**
```
Chrome tab 3: Google — textbox "Search" focused
```

### Token Budget Targets

| Response Type | Target | Current |
|---------------|--------|---------|
| Action confirmation | < 50 tokens | ~200 tokens |
| Quick state (context, status) | < 30 tokens | ~150 tokens |
| Page scan (web_tree interactive) | < 200 tokens | ~2000 tokens |
| Full tree (web_tree full) | < 2000 tokens | ~2000 tokens |
| Element search (find) | < 100 tokens | ~500 tokens |

**10x reduction on most operations.**

## Section 6: Reliability — Zero Silent Failures

### Every tool returns one of three states:

- **SUCCESS** → result + confirmation
- **FAILED** → error code + what went wrong + what to try instead
- **DEGRADED** → partial result + what's missing + why

### Top Failure Mode Fixes

| Failure | Current | New |
|---------|---------|-----|
| CDP disconnected | Silent fail/hang | Auto-reconnect (1 retry, 2s), explicit error |
| Element ID expired | Click wrong element | Validate before acting, clear error if stale |
| Chrome not on debug port | Cryptic error | Actionable message with install reference |
| Accessibility denied | Partial/empty tree | Specific OS instructions |
| Tab closed between actions | Silent fail | List current tabs in error |
| JS injection fails | Empty tree | Retry once, fallback to native AX with notice |
| Element offscreen | Wrong coordinates | Auto-scroll into view first |
| Field readonly/disabled | Silent no-op | Explicit error |

### Timeouts

| Operation | Timeout |
|-----------|---------|
| CDP command | 5s |
| Page navigation | 15s (was 30s) |
| Element wait | 10s default (configurable) |
| Tree extraction | 3s (was unlimited) |
| Auto-reconnect | 2s (new) |

### Registry Improvements

- Tied to page state, not TTL timer — invalidate on navigation
- Stale ID access returns: `"✗ element [7] expired (page changed). Run web_tree to refresh."`
- Cap at 500 elements (interactive-only makes this easy)

## Section 7: Shadow DOM Piercing

The #1 gap in accessibility-tree approaches. Every competitor struggles here.

### Three-Layer Extraction

```
Layer 1: Accessibility.getFullAXTree() → everything outside shadow roots
Layer 2: DOM.getFlattenedDocument(pierce: true) → everything inside shadow roots
Layer 3: Merge, deduplicate, assign element IDs
```

### Result for the Agent

Before (shadow DOM invisible):
```
[1] button "Menu"
[2] link "Home"
    ← missing: 15 elements inside <custom-nav> shadow root
[3] textbox "Search"
```

After (shadow DOM pierced):
```
[1] button "Menu"
[2] link "Home"
[3] link "Products"        ← was in shadow root
[4] link "Pricing"         ← was in shadow root
[5] link "Docs"            ← was in shadow root
[6] textbox "Search"
```

### Competitive Position

| Tool | Shadow DOM | How |
|------|-----------|-----|
| Playwright MCP | ✗ Broken | No solution |
| Browser-Use | ~Partial | Vision fallback (expensive) |
| Chrome DevTools MCP | ~Partial | Manual CDP |
| **agent-eyes v0.5** | **✓ Full** | **CDP pierce, zero images** |

## Section 8: Cross-Platform Parity

Three platforms, three native stacks, one consistent API.

### Platform Changes

| Platform | Current Input | Target Input | Why |
|----------|--------------|-------------|-----|
| macOS | CGEventPost | CGEventPost (keep) | Already native, fast |
| Linux | xdotool subprocess | python-xlib | Native Python, no subprocess overhead |
| Windows | basic pywinauto | comtypes + UI Automation | Full control, better tree extraction |

### Optional Dependencies (pyproject.toml)

```toml
[project.optional-dependencies]
macos = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Quartz", "pyobjc-framework-Cocoa", "pyobjc-framework-Vision"]
linux = ["python-xlib>=0.33"]
windows = ["comtypes>=1.4.0"]
```

### Adapter Protocol (Enforced)

```python
class PlatformAdapter(Protocol):
    def get_tree(pid, max_depth) -> list[UIElement]
    def find(pid, role, name) -> list[UIElement]
    def click(x, y, button) -> None
    def double_click(x, y) -> None
    def type_text(text, human_like) -> None
    def press_key(key, modifiers) -> None
    def scroll(x, y, dx, dy) -> None
    def drag(x1, y1, x2, y2) -> None
    def list_apps() -> list[AppInfo]
    def get_focused() -> UIElement | None
    def activate_window(pid) -> None
```

No optional methods. No `NotImplementedError`. Every platform implements everything.

## Section 9: Phased Rollout

### Phase 1: Bulletproof Core (v0.5.0)

| # | Task | Impact | Effort |
|---|------|--------|--------|
| 1.1 | Kill extension tier, `element_at`, `get_ocr_hints`, `setup`/`setup_apply` | Less code, fewer bugs | S |
| 1.2 | New response format — flat text, one-liners for actions | 10x token reduction | M |
| 1.3 | `interactive_only=true` default for `web_tree`/`tree` | 80% token reduction | M |
| 1.4 | Explicit error responses — never silent, always actionable | Kills 25-45% failure rate | M |
| 1.5 | Registry tied to page state, not TTL timer | No stale element clicks | S |
| 1.6 | CDP auto-reconnect (1 retry, 2s) | No connection hangs | S |
| 1.7 | Rename all tools — drop `eyes_` prefix | Cleaner API, fewer tokens | S |
| 1.8 | Timeout enforcement on all operations | No hangs | S |

### Phase 2: Competitive Features (v0.6.0)

| # | Task | Impact | Effort |
|---|------|--------|--------|
| 2.1 | Shadow DOM piercing via CDP `pierce: true` | #1 competitive gap closed | L |
| 2.2 | Plugin architecture — `.claude-plugin/`, marketplace | Reliable slash commands | M |
| 2.3 | `/agent-eyes:install` skill | Zero-friction onboarding | M |
| 2.4 | Updated `/agent-eyes:init` — checks install state | Single entry point | S |
| 2.5 | `pierce` tool for targeted shadow root inspection | Advanced use cases | S |

### Phase 3: Platform Parity (v0.7.0)

| # | Task | Impact | Effort |
|---|------|--------|--------|
| 3.1 | Linux: replace xdotool with python-xlib | Faster, no subprocess | M |
| 3.2 | Windows: direct UI Automation via comtypes | Full parity | L |
| 3.3 | Linux/Windows: implement scroll and drag | Complete adapter | M |
| 3.4 | Adapter Protocol enforcement | Guaranteed parity | S |
| 3.5 | Platform-specific self-test in install | Verify per-OS | S |
| 3.6 | `pyproject.toml` optional deps | Clean dep separation | S |

### Explicitly NOT Doing (YAGNI)

- Vision/OCR fallback — against core philosophy
- Workflow recording/replay — nice-to-have, not core
- Mobile support (iOS/Android) — different problem space
- Annotated screenshots — no images, ever
- Canvas/WebGL scene extraction — too niche, too fragile

## Competitive Positioning

- **vs Playwright MCP:** "We do browsers + native desktop apps. One tool for everything."
- **vs Claude Computer Use:** "10-100x faster, 10x cheaper. No vision model needed."
- **vs Terminator:** "Cross-platform (macOS + Windows + Linux) vs Windows-only. Plus browser automation."
- **vs Browser-Use:** "Faster, cheaper, deterministic. Works with desktop apps too."

## Research Sources

- Playwright MCP, Chrome DevTools MCP, Browser-Use, Stagehand, agent-browser (Vercel)
- Terminator, FlaUI MCP, DirectShell, UI Automata, CoDriver MCP
- Claude Computer Use, OpenAI Operator/CUA, Simular AI, AskUI
- Mind2Web (NeurIPS 2023), WebVoyager, OpenCUA benchmarks
- 30+ competitor tools analyzed across browser, desktop, and hybrid categories
