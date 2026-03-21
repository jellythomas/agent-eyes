"""Database of known competing MCP servers that agent-eyes can replace."""

from __future__ import annotations

# Each competitor has:
#   id: unique identifier for selection
#   name: human-readable name
#   category: "browser" | "desktop" | "vision"
#   detection: patterns to match in MCP configs
#     - keys: server name keys in config (case-insensitive substring match)
#     - args: strings to match in command/args arrays
#     - commands: executable names to match
#   tools_prefix: tool name prefix this server uses
#   tools_count: approximate number of tools
#   overlap: what agent-eyes capabilities it overlaps with

COMPETITORS: list[dict] = [
    # ── Browser Automation ──────────────────────────────────────────
    {
        "id": "playwright-mcp",
        "name": "Playwright MCP",
        "category": "browser",
        "detection": {
            "keys": ["playwright"],
            "args": ["@playwright/mcp", "playwright-mcp"],
            "commands": [],
        },
        "tools_prefix": "browser_",
        "tools_count": 22,
        "overlap": [
            "eyes_get_web_tree", "eyes_click", "eyes_type", "eyes_navigate",
            "eyes_evaluate", "eyes_press_key", "eyes_hover", "eyes_drag",
            "eyes_fill_form", "eyes_file_upload", "eyes_wait_for",
            "eyes_handle_dialog", "eyes_list_chrome_tabs", "eyes_close_tab",
        ],
    },
    {
        "id": "executeautomation-playwright",
        "name": "ExecuteAutomation Playwright MCP",
        "category": "browser",
        "detection": {
            "keys": ["executeautomation"],
            "args": ["@executeautomation/playwright-mcp-server"],
            "commands": [],
        },
        "tools_prefix": "playwright_",
        "tools_count": 32,
        "overlap": [
            "eyes_get_web_tree", "eyes_click", "eyes_type", "eyes_navigate",
            "eyes_evaluate", "eyes_fill_form",
        ],
    },
    {
        "id": "puppeteer-mcp",
        "name": "Puppeteer MCP (archived)",
        "category": "browser",
        "detection": {
            "keys": ["puppeteer"],
            "args": [
                "@modelcontextprotocol/server-puppeteer",
                "puppeteer-mcp-server",
            ],
            "commands": [],
        },
        "tools_prefix": "puppeteer_",
        "tools_count": 7,
        "overlap": [
            "eyes_navigate", "eyes_click", "eyes_type", "eyes_evaluate",
            "eyes_get_web_tree",
        ],
    },
    {
        "id": "browserbase-mcp",
        "name": "Browserbase MCP (Stagehand)",
        "category": "browser",
        "detection": {
            "keys": ["browserbase"],
            "args": ["@browserbasehq/mcp-server-browserbase", "browserbase"],
            "commands": [],
        },
        "tools_prefix": "browserbase_",
        "tools_count": 10,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_navigate", "eyes_get_web_tree",
        ],
    },
    {
        "id": "steel-mcp",
        "name": "Steel Browser MCP",
        "category": "browser",
        "detection": {
            "keys": ["steel"],
            "args": ["steel-mcp-server", "steel-voyager"],
            "commands": [],
        },
        "tools_prefix": "steel_",
        "tools_count": 8,
        "overlap": ["eyes_navigate", "eyes_click", "eyes_type"],
    },
    {
        "id": "chrome-devtools-mcp",
        "name": "Chrome DevTools MCP",
        "category": "browser",
        "detection": {
            "keys": ["chrome-devtools", "devtools"],
            "args": ["chrome-devtools-mcp"],
            "commands": [],
        },
        "tools_prefix": "devtools_",
        "tools_count": 15,
        "overlap": [
            "eyes_evaluate", "eyes_get_web_tree", "eyes_navigate",
        ],
    },
    {
        "id": "browser-use-mcp",
        "name": "browser-use MCP",
        "category": "browser",
        "detection": {
            "keys": ["browser-use", "browser_use"],
            "args": ["browser-use-mcp", "browser-use-mcp-server"],
            "commands": [],
        },
        "tools_prefix": "browser_",
        "tools_count": 8,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_navigate", "eyes_get_web_tree",
        ],
    },
    {
        "id": "selenium-mcp",
        "name": "Selenium MCP",
        "category": "browser",
        "detection": {
            "keys": ["selenium"],
            "args": [
                "@angiejones/mcp-selenium",
                "@sirblob/mcp-selenium",
                "selenium-mcp-server",
                "mcp-selenium",
            ],
            "commands": [],
        },
        "tools_prefix": "selenium_",
        "tools_count": 12,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_navigate", "eyes_fill_form",
        ],
    },
    {
        "id": "agent-infra-browser",
        "name": "Agent Infra Browser MCP",
        "category": "browser",
        "detection": {
            "keys": ["agent-infra"],
            "args": ["@agent-infra/mcp-server-browser"],
            "commands": [],
        },
        "tools_prefix": "browser_",
        "tools_count": 10,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_navigate", "eyes_get_web_tree",
        ],
    },
    # ── Desktop / OS Automation ─────────────────────────────────────
    {
        "id": "desktop-commander",
        "name": "Desktop Commander MCP",
        "category": "desktop",
        "detection": {
            "keys": ["desktop-commander"],
            "args": ["@wonderwhy-er/desktop-commander"],
            "commands": [],
        },
        "tools_prefix": "execute_",
        "tools_count": 10,
        "overlap": ["eyes_app", "eyes_press_key"],
    },
    {
        "id": "mcp-desktop-automation",
        "name": "Desktop Automation MCP (RobotJS)",
        "category": "desktop",
        "detection": {
            "keys": ["desktop-automation", "mcp-desktop-automation"],
            "args": ["mcp-desktop-automation"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 8,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_press_key", "eyes_scroll",
            "eyes_get_ocr_hints",
        ],
    },
    {
        "id": "mcp-control",
        "name": "MCPControl (nut.js)",
        "category": "desktop",
        "detection": {
            "keys": ["mcp-control", "mcpcontrol"],
            "args": ["mcp-control"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 10,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_press_key", "eyes_window",
            "eyes_scroll",
        ],
    },
    {
        "id": "windows-mcp",
        "name": "Windows-MCP (CursorTouch)",
        "category": "desktop",
        "detection": {
            "keys": ["windows-mcp", "cursortouch"],
            "args": ["windows-mcp"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 15,
        "overlap": [
            "eyes_get_tree", "eyes_click", "eyes_type", "eyes_app",
            "eyes_window",
        ],
    },
    {
        "id": "computer-use-mcp",
        "name": "computer-use MCP",
        "category": "desktop",
        "detection": {
            "keys": ["computer-use", "computer_use"],
            "args": ["computer-use-mcp"],
            "commands": [],
        },
        "tools_prefix": "computer_",
        "tools_count": 6,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_press_key", "eyes_get_ocr_hints",
        ],
    },
    {
        "id": "computer-control-mcp",
        "name": "Computer Control MCP (PyAutoGUI)",
        "category": "desktop",
        "detection": {
            "keys": ["computer-control"],
            "args": ["computer-control-mcp"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 8,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_press_key", "eyes_get_ocr_hints",
        ],
    },
    {
        "id": "system-mcp",
        "name": "System MCP (Cross-Platform)",
        "category": "desktop",
        "detection": {
            "keys": ["system_mcp", "system-mcp"],
            "args": ["system_mcp", "system-mcp"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 12,
        "overlap": [
            "eyes_get_tree", "eyes_click", "eyes_type", "eyes_app",
            "eyes_window", "eyes_press_key",
        ],
    },
    {
        "id": "macos-automator-mcp",
        "name": "macOS Automator MCP",
        "category": "desktop",
        "detection": {
            "keys": ["macos-automator", "macos_automator"],
            "args": [
                "@steipete/macos-automator-mcp",
                "macos-automator-mcp",
            ],
            "commands": [],
        },
        "tools_prefix": "run_",
        "tools_count": 5,
        "overlap": ["eyes_app", "eyes_evaluate"],
    },
    {
        "id": "macos-gui-mcp",
        "name": "macOS GUI Control MCP",
        "category": "desktop",
        "detection": {
            "keys": ["macos-gui", "macos_gui", "macos-control", "macoscontrol"],
            "args": ["macos-gui-mcp", "mcp-macoscontrol"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 10,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_press_key", "eyes_window",
            "eyes_get_ocr_hints",
        ],
    },
    {
        "id": "windows-desktop-automation",
        "name": "Windows Desktop Automation (AutoIt)",
        "category": "desktop",
        "detection": {
            "keys": ["windows-desktop-automation"],
            "args": ["mcp-windows-desktop-automation"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 10,
        "overlap": [
            "eyes_click", "eyes_type", "eyes_window", "eyes_app",
        ],
    },
    # ── Screen / Vision ─────────────────────────────────────────────
    {
        "id": "peekaboo",
        "name": "Peekaboo (macOS Screen Capture)",
        "category": "vision",
        "detection": {
            "keys": ["peekaboo"],
            "args": ["@steipete/peekaboo"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 3,
        "overlap": ["eyes_get_ocr_hints", "eyes_list_apps", "eyes_context"],
    },
    {
        "id": "screenpipe-mcp",
        "name": "Screenpipe MCP",
        "category": "vision",
        "detection": {
            "keys": ["screenpipe"],
            "args": ["screenpipe-mcp"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 5,
        "overlap": ["eyes_get_ocr_hints"],
    },
    {
        "id": "screen-vision-mcp",
        "name": "Screen Vision MCP",
        "category": "vision",
        "detection": {
            "keys": ["screen-vision"],
            "args": ["screen-vision-mcp"],
            "commands": [],
        },
        "tools_prefix": "",
        "tools_count": 3,
        "overlap": ["eyes_get_ocr_hints", "eyes_get_tree"],
    },
]


def get_competitor_by_id(competitor_id: str) -> dict | None:
    """Look up a competitor by its ID."""
    for c in COMPETITORS:
        if c["id"] == competitor_id:
            return c
    return None


def get_competitors_by_category(category: str) -> list[dict]:
    """Get all competitors in a category."""
    return [c for c in COMPETITORS if c["category"] == category]


CATEGORIES = {
    "browser": "Browser Automation",
    "desktop": "Desktop / OS Automation",
    "vision": "Screen / Vision",
}
