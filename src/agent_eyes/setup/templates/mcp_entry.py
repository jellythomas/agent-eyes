"""MCP server config entries for different AI tools."""

from __future__ import annotations

from pathlib import Path


def _executable_path(executable: str | Path) -> str:
    path = Path(executable).expanduser()
    if not path.is_absolute():
        raise ValueError("Agent Eyes MCP executable must be an absolute path")
    return str(path)


def get_mcp_entry(executable: str | Path) -> dict:
    """Return the agent-eyes MCP server config entry.

    Works for: Claude Desktop, Claude Code, Cursor, Cline, Roo Code, Windsurf.
    """
    return {
        "command": _executable_path(executable),
        "args": ["serve"],
    }


def get_mcp_entry_zed(executable: str | Path) -> dict:
    """Return agent-eyes config for Zed (uses different structure)."""
    return {
        "source": "custom",
        "command": _executable_path(executable),
        "args": ["serve"],
    }


# ── Tool list for Claude Code agent definitions ─────────────────────

AGENT_EYES_TOOLS = [
    "mcp__agent-eyes__status",
    "mcp__agent-eyes__list_apps",
    "mcp__agent-eyes__tree",
    "mcp__agent-eyes__find",
    "mcp__agent-eyes__click",
    "mcp__agent-eyes__type",
    "mcp__agent-eyes__focused",
    "mcp__agent-eyes__list_tabs",
    "mcp__agent-eyes__web_tree",
    "mcp__agent-eyes__navigate",
    "mcp__agent-eyes__js",
    "mcp__agent-eyes__press_key",
    "mcp__agent-eyes__wait",
    "mcp__agent-eyes__new_tab",
    "mcp__agent-eyes__close_tab",
    "mcp__agent-eyes__dialog",
    "mcp__agent-eyes__upload",
    "mcp__agent-eyes__scroll",
    "mcp__agent-eyes__drag",
    "mcp__agent-eyes__fill_form",
    "mcp__agent-eyes__hover",
    "mcp__agent-eyes__app",
    "mcp__agent-eyes__subtree",
    "mcp__agent-eyes__window",
    "mcp__agent-eyes__context",
    "mcp__agent-eyes__shadow",
    "mcp__agent-eyes__pierce",
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
