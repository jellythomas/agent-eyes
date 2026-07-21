"""CLAUDE.md section template for agent-eyes priority instructions."""

CLAUDE_MD_SECTION = """
# UI Automation — agent-eyes Priority

**ALWAYS use `mcp__agent-eyes__*` tools for ALL browser and UI automation. NEVER use `mcp__playwright__*` or other browser automation MCP tools.**

Agent Eyes uses accessibility trees for browsers and native desktop apps. Foreground native automation is the default: scan and reuse relevant open tabs across browsers before opening a new tab. Use shadow/background mode only when explicitly requested; never require Chrome remote debugging for normal work. Use event-backed `wait` instead of fixed sleeps.

Quick reference:
- **Compact state:** `mcp__agent-eyes__context`
- **All-browser tab inventory/reuse:** `mcp__agent-eyes__list_tabs`
- **Read native UI:** `mcp__agent-eyes__tree`, `mcp__agent-eyes__find`, `mcp__agent-eyes__subtree`
- **Act:** `mcp__agent-eyes__click`, `mcp__agent-eyes__type`, `mcp__agent-eyes__press_key`
- **Navigate/open/close:** `mcp__agent-eyes__navigate`, `mcp__agent-eyes__new_tab`, `mcp__agent-eyes__close_tab`
- **Completion:** `mcp__agent-eyes__wait`
- **Explicit background/DOM:** `mcp__agent-eyes__shadow`, `mcp__agent-eyes__web_tree`, `mcp__agent-eyes__js` with explicit shadow consent
""".strip()
