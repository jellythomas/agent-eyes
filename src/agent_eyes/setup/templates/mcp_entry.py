"""MCP server config entries for different AI tools."""

from __future__ import annotations


def get_mcp_entry() -> dict:
    """Return the agent-eyes MCP server config entry.

    Works for: Claude Desktop, Claude Code, Cursor, Cline, Roo Code, Windsurf.
    """
    return {
        "command": "uvx",
        "args": ["agent-eyes"],
    }


def get_mcp_entry_zed() -> dict:
    """Return agent-eyes config for Zed (uses different structure)."""
    return {
        "source": "custom",
        "command": "uvx",
        "args": ["agent-eyes"],
    }


# ── Tool list for Claude Code agent definitions ─────────────────────

AGENT_EYES_TOOLS = [
    "mcp__agent-eyes__eyes_status",
    "mcp__agent-eyes__eyes_context",
    "mcp__agent-eyes__eyes_list_apps",
    "mcp__agent-eyes__eyes_get_focused",
    "mcp__agent-eyes__eyes_get_tree",
    "mcp__agent-eyes__eyes_get_subtree",
    "mcp__agent-eyes__eyes_find",
    "mcp__agent-eyes__eyes_element_at",
    "mcp__agent-eyes__eyes_get_ocr_hints",
    "mcp__agent-eyes__eyes_click",
    "mcp__agent-eyes__eyes_type",
    "mcp__agent-eyes__eyes_press_key",
    "mcp__agent-eyes__eyes_hover",
    "mcp__agent-eyes__eyes_scroll",
    "mcp__agent-eyes__eyes_drag",
    "mcp__agent-eyes__eyes_fill_form",
    "mcp__agent-eyes__eyes_file_upload",
    "mcp__agent-eyes__eyes_wait_for",
    "mcp__agent-eyes__eyes_app",
    "mcp__agent-eyes__eyes_window",
    "mcp__agent-eyes__eyes_list_chrome_tabs",
    "mcp__agent-eyes__eyes_get_web_tree",
    "mcp__agent-eyes__eyes_navigate",
    "mcp__agent-eyes__eyes_evaluate",
    "mcp__agent-eyes__eyes_new_tab",
    "mcp__agent-eyes__eyes_close_tab",
    "mcp__agent-eyes__eyes_handle_dialog",
    "mcp__agent-eyes__eyes_shadow",
]


def get_agent_eyes_tools_list() -> str:
    """Return comma-separated tools string for agent definitions."""
    return ", ".join(AGENT_EYES_TOOLS)


# ── Competitor tool patterns for replacement in agent files ──────────

COMPETITOR_TOOL_PATTERNS = {
    "playwright-mcp": r"mcp__playwright__\w+",
    "puppeteer-mcp": r"mcp__puppeteer__\w+",
    "browserbase-mcp": r"mcp__browserbase__\w+",
    "browser-use-mcp": r"mcp__browser-use__\w+",
    "selenium-mcp": r"mcp__selenium__\w+",
    "desktop-commander": r"mcp__desktop-commander__\w+",
    "computer-use-mcp": r"mcp__computer-use__\w+",
    "computer-control-mcp": r"mcp__computer-control__\w+",
    "peekaboo": r"mcp__peekaboo__\w+",
    "screenpipe-mcp": r"mcp__screenpipe__\w+",
}
