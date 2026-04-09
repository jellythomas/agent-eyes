"""MCP Eyes — Accessibility-tree vision for AI agents.

Claude's eyes: see and interact with ANY application through
accessibility trees — no screenshots needed. The tree IS the vision.

Cross-platform: macOS (AXUIElement), Windows (UI Automation),
Linux (AT-SPI2), plus Chrome DevTools Protocol for web content
enriched with bounding boxes and visual metadata.
"""
from __future__ import annotations

import json
import sys
import time
import asyncio
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .adapters.base import BaseAdapter, UIElement
from .cdp import CDPClient
from .cdp_persistent import CDPConnection as PersistentCDP
from .tiers import TierManager, ConnectionTier
from .registry import ElementRegistry
from . import platform_utils as _pu
from .__init__ import __version__
from .input_sim import get_input_backend
from .js_bridge import build_ax_tree_script, format_ax_tree

# AppleScript is macOS-only — import conditionally
if sys.platform == "darwin":
    from . import applescript as _as
else:
    _as = None  # type: ignore

logger = logging.getLogger("agent-eyes")

# ── First-run auto-setup (once per process) ─────────────────────────
_setup_checked = False


def _maybe_auto_setup() -> str | None:
    """Check if setup is needed. Returns a short reminder nudging the user
    to run the init command, or None if already configured.
    Runs at most once per process."""
    global _setup_checked
    if _setup_checked:
        return None
    _setup_checked = True
    try:
        from .setup.state import is_first_run, needs_rescan
        if is_first_run():
            return (
                "⚠️ agent-eyes is not configured yet.\n"
                "Run `/agent-eyes-init` to set up agent-eyes and replace competing MCP servers.\n"
                "This only takes a few seconds and uses interactive setup."
            )
        elif needs_rescan(__version__):
            return (
                "ℹ️ agent-eyes has been upgraded to a new version.\n"
                "Run `/agent-eyes-init` to re-scan and update your configuration."
            )
    except Exception as e:
        logger.debug("Auto-setup skipped: %s", e)
    return None

# ── Platform detection ──────────────────────────────────────────────
def _auto_install_platform_deps() -> None:
    """Install missing platform-specific dependencies at runtime.

    When installed via ``uvx agent-eyes`` from a PyPI version that predates
    the platform-conditional dependencies, the native adapter packages may
    be absent.  This function detects the gap and installs them into the
    running environment so a server restart is not required.

    Supports both ``uv``-managed environments (uvx) and plain pip.
    """
    import shutil
    import subprocess

    def _pip_install(packages: list[str]) -> None:
        """Install packages, preferring ``uv pip`` (works in uvx envs)."""
        logger.info("Installing platform dependencies: %s", ", ".join(packages))
        uv = shutil.which("uv")
        if uv:
            subprocess.check_call(
                [uv, "pip", "install", "--python", sys.executable, *packages],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *packages],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

    if sys.platform == "darwin":
        needed: list[str] = []
        failed_mods: list[str] = []
        for mod, pkg in [
            ("ApplicationServices", "pyobjc-framework-ApplicationServices"),
            ("Quartz", "pyobjc-framework-Quartz"),
            ("Cocoa", "pyobjc-framework-Cocoa"),
        ]:
            try:
                __import__(mod)
            except ImportError:
                needed.append(pkg)
                failed_mods.append(mod)
        if needed:
            logger.info("Auto-installing macOS native deps: %s", ", ".join(needed))
            _pip_install(needed)
            # Remove failed-import sentinels from sys.modules so
            # the retry import can actually find the new packages.
            for mod in failed_mods:
                sys.modules.pop(mod, None)
    elif sys.platform == "win32":
        try:
            __import__("pywinauto")
        except ImportError:
            logger.info("Auto-installing Windows native dep: pywinauto")
            _pip_install(["pywinauto"])
            sys.modules.pop("pywinauto", None)


def _get_native_adapter() -> BaseAdapter | None:
    """Auto-detect and return the platform adapter.

    Attempts to load the native adapter first; if dependencies are missing,
    auto-installs them and retries once.
    """
    def _try_load() -> BaseAdapter | None:
        if sys.platform == "darwin":
            from .adapters.macos import MacOSAdapter
            adapter = MacOSAdapter()
            if adapter.is_available():
                return adapter
        elif sys.platform == "win32":
            from .adapters.windows import WindowsAdapter
            adapter = WindowsAdapter()
            if adapter.is_available():
                return adapter
        elif sys.platform == "linux":
            from .adapters.linux import LinuxAdapter
            adapter = LinuxAdapter()
            if adapter.is_available():
                return adapter
        return None

    adapter = _try_load()
    if adapter is not None:
        return adapter

    # Dependencies missing — try to install and retry
    try:
        _auto_install_platform_deps()
    except Exception as exc:
        logger.warning("Auto-install of platform deps failed: %s", exc)
        return None

    # After installing new packages via subprocess, Python's import
    # machinery won't find them until we invalidate the finder caches.
    # Without this, the retry below fails even though packages exist on disk.
    import importlib
    importlib.invalidate_caches()

    return _try_load()


# ── Server setup ────────────────────────────────────────────────────
app = Server("agent-eyes")
registry = ElementRegistry()
native_adapter = _get_native_adapter()
cdp_client = CDPClient()
tier_manager = TierManager()
cdp_pool = PersistentCDP()
_input_backend = get_input_backend()  # Singleton — no need to re-probe on every call


def _platform_status() -> str:
    parts = []
    if native_adapter:
        ok, msg = native_adapter.check_permissions()
        parts.append(f"Native adapter: {native_adapter.__class__.__name__} — {msg}")
    else:
        parts.append("Native adapter: NOT AVAILABLE (missing dependencies)")
    parts.append("CDP (Chrome): check with list_tabs")
    return "\n".join(parts)


# ── Tool definitions ────────────────────────────────────────────────
TOOLS = [
    Tool(
        name="status",
        description=(
            "Check agent-eyes status: platform adapter, permissions, CDP availability. "
            "Call this first to verify the server is working."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="list_apps",
        description=(
            "List all running applications with visible windows. "
            "Returns PID, name, bundle ID, window titles. "
            "Use the PID to call tree."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="tree",
        description=(
            "Get the accessibility tree of an application by PID. "
            "Returns a numbered text representation of ALL UI elements — "
            "buttons, text fields, headings, tables, etc. "
            "Each element has an [id] you can use with click/type. "
            "This is the PRIMARY way to 'see' an application — no screenshot needed. "
            "For Chrome/Chromium browsers, automatically includes web page content "
            "(headings, buttons, inputs, links, chat items) via AppleScript on macOS "
            "or CDP on all platforms (macOS/Linux/Windows). "
            "For large apps, use subtree to drill into specific sections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "description": "Process ID of the application",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max tree depth (default 10, max 20)",
                    "default": 10,
                },
            },
            "required": ["pid"],
        },
    ),
    Tool(
        name="find",
        description=(
            "Search for UI elements by role, name, or value within an app. "
            "Searches the currently loaded tree (call tree first) "
            "or specify a PID to load fresh. Returns matching elements with IDs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "description": "Process ID (optional if tree already loaded)",
                },
                "role": {
                    "type": "string",
                    "description": "Element role to match (e.g. 'button', 'textfield', 'link')",
                },
                "name": {
                    "type": "string",
                    "description": "Element name/title to match (partial, case-insensitive)",
                },
                "value": {
                    "type": "string",
                    "description": "Element value to match (partial, case-insensitive)",
                },
                "match": {
                    "type": "string",
                    "description": "Match type: 'contains' (default), 'exact', 'regex', 'prefix', 'suffix'",
                    "default": "contains",
                    "enum": ["contains", "exact", "regex", "prefix", "suffix"],
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="click",
        description=(
            "Click/press a UI element by its [id] from the tree. "
            "Works for buttons, links, checkboxes, menu items, etc. "
            "Alternatively, click by screen coordinates (x, y) with a target pid."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Element ID from the accessibility tree",
                },
                "x": {
                    "type": "integer",
                    "description": "Screen X coordinate (use with y and pid for coordinate click)",
                },
                "y": {
                    "type": "integer",
                    "description": "Screen Y coordinate (use with x and pid for coordinate click)",
                },
                "pid": {
                    "type": "integer",
                    "description": "Target app PID (required for coordinate click)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="type",
        description=(
            "Type text into a UI element (text field, search box, etc.) by its [id]. "
            "Uses human-like keyboard simulation (real key events) when possible, "
            "which triggers all event listeners. Falls back to programmatic set_value."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Element ID from the accessibility tree",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the element",
                },
            },
            "required": ["id", "text"],
        },
    ),
    Tool(
        name="focused",
        description=(
            "Get the currently focused UI element across all apps. "
            "Useful to see what's active without knowing which app/PID."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="list_tabs",
        description=(
            "List all Chrome browser tabs. Returns tab ID, title, URL. "
            "Uses CDP (Chrome DevTools Protocol) with auto-discovery of debug port. "
            "On macOS, falls back to AppleScript if CDP unavailable. "
            "On Linux/Windows, requires Chrome started with --remote-debugging-port. "
            "Use tab ID with web_tree for richer web content access."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="web_tree",
        description=(
            "Get the accessibility tree of a Chrome tab via CDP. "
            "Richer than native AX for web content — gets full semantic structure. "
            "Requires Chrome with --remote-debugging-port=9222."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index from list_tabs (0-based)",
                    "default": 0,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max tree depth (default 5)",
                    "default": 5,
                },
            },
            "required": [],
        },
    ),
    # ── New tools ──────────────────────────────────────────────────
    Tool(
        name="navigate",
        description=(
            "Navigate a Chrome tab to a URL. Opens the URL in the specified tab "
            "(default: current tab). Waits for the page to load."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to",
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0 = current tab)",
                    "default": 0,
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="js",
        description=(
            "Execute JavaScript in a Chrome tab and return the result. "
            "Supports async expressions (await). Returns the evaluated value."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate",
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0)",
                    "default": 0,
                },
            },
            "required": ["expression"],
        },
    ),
    Tool(
        name="press_key",
        description=(
            "Press a keyboard key in any application (native or web). "
            "For native apps, provide a PID to target that app. "
            "For Chrome/web, optionally provide tab_index. "
            "Supports special keys (Enter, Tab, Escape, Backspace, Delete, "
            "ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, F1-F12, Space) "
            "and modifiers (Ctrl, Alt, Meta/Cmd, Shift). "
            "For typing text into a field, use type instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g. 'Enter', 'Tab', 'Escape', 'a')",
                },
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Modifier keys: 'Ctrl', 'Alt', 'Meta', 'Shift'",
                },
                "pid": {
                    "type": "integer",
                    "description": "Target app PID for native apps. If omitted, targets Chrome tab.",
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Chrome tab index (default 0). Only used when targeting Chrome.",
                    "default": 0,
                },
            },
            "required": ["key"],
        },
    ),
    Tool(
        name="wait",
        description=(
            "Wait for a web element to appear in the accessibility tree. "
            "Polls until the element is found or timeout is reached."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Element role to wait for (e.g. 'button', 'heading')",
                },
                "name": {
                    "type": "string",
                    "description": "Element name/label to wait for",
                },
                "timeout": {
                    "type": "number",
                    "description": "Max seconds to wait (default 5)",
                    "default": 5,
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0)",
                    "default": 0,
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID for native app polling (alternative to Chrome tab)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="new_tab",
        description=(
            "Open a new Chrome tab, optionally navigating to a URL. "
            "Returns the new tab's info."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open (default: about:blank)",
                    "default": "about:blank",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="close_tab",
        description=(
            "Close a Chrome tab by title match or index. "
            "IMPORTANT: Always call list_tabs first to verify "
            "which tab to close. Prefer using 'title' over 'tab_index' for safety."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Substring to match against tab titles (case-insensitive). "
                        "Preferred over tab_index for safe targeting."
                    ),
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index to close. Use only after listing tabs.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="dialog",
        description=(
            "Handle a JavaScript dialog (alert, confirm, prompt). "
            "Accept or dismiss it, optionally providing text for prompts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "accept": {
                    "type": "boolean",
                    "description": "True to accept/OK, False to dismiss/Cancel (default true)",
                    "default": True,
                },
                "prompt_text": {
                    "type": "string",
                    "description": "Text to enter for prompt dialogs",
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0)",
                    "default": 0,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="upload",
        description=(
            "Upload file(s) to a file input element by its [id]. "
            "Provide absolute file paths."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Element ID of the file input from the web tree",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute file paths to upload",
                },
            },
            "required": ["id", "files"],
        },
    ),
    Tool(
        name="scroll",
        description=(
            "Scroll the page in a Chrome tab. Use positive delta_y to scroll down, "
            "negative to scroll up. Coordinates specify where to scroll from."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "delta_y": {
                    "type": "integer",
                    "description": "Vertical scroll amount (positive=down, negative=up). Default 300.",
                    "default": 300,
                },
                "delta_x": {
                    "type": "integer",
                    "description": "Horizontal scroll amount (positive=right, negative=left). Default 0.",
                    "default": 0,
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate to scroll from (default 400)",
                    "default": 400,
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate to scroll from (default 400)",
                    "default": 400,
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0)",
                    "default": 0,
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID of a native app to scroll in (omit for browser tabs).",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="drag",
        description=(
            "Drag and drop from one point to another in a Chrome tab. "
            "Simulates smooth mouse movement."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "from_x": {"type": "integer", "description": "Start X coordinate"},
                "from_y": {"type": "integer", "description": "Start Y coordinate"},
                "to_x": {"type": "integer", "description": "End X coordinate"},
                "to_y": {"type": "integer", "description": "End Y coordinate"},
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index (default 0)",
                    "default": 0,
                },
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
    ),
    Tool(
        name="fill_form",
        description=(
            "Fill multiple form fields at once. Each field is identified by its [id] "
            "from the web accessibility tree."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Element ID from web tree",
                            },
                            "value": {
                                "type": "string",
                                "description": "Value to fill",
                            },
                        },
                        "required": ["id", "value"],
                    },
                    "description": "List of {id, value} pairs to fill",
                },
            },
            "required": ["fields"],
        },
    ),
    Tool(
        name="hover",
        description=(
            "Hover over a UI element to trigger tooltips, dropdown previews, "
            "or CSS :hover states. Moves the mouse to the element's center."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Element ID from the accessibility tree",
                },
                "x": {
                    "type": "integer",
                    "description": "Screen X coordinate (alternative to id)",
                },
                "y": {
                    "type": "integer",
                    "description": "Screen Y coordinate (alternative to id)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="app",
        description=(
            "Launch, quit, or switch to an application. "
            "Actions: 'launch' (by name or bundle ID), 'quit', 'focus' (bring to front)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'launch', 'quit', or 'focus'",
                    "enum": ["launch", "quit", "focus"],
                },
                "name": {
                    "type": "string",
                    "description": "App name (e.g. 'Safari') or bundle ID (e.g. 'com.apple.Safari')",
                },
            },
            "required": ["action", "name"],
        },
    ),
    Tool(
        name="subtree",
        description=(
            "Get the accessibility subtree rooted at a specific element. "
            "Use to drill into complex UIs without loading the entire tree. "
            "Much more efficient than re-fetching the full tree with higher depth."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Element ID to expand (from a previous tree call)",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "How many levels deep to expand (default 5)",
                    "default": 5,
                },
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="window",
        description=(
            "Manage application windows. Actions: 'list' (all windows with positions), "
            "'focus' (bring to front), 'minimize', 'close', 'move' (x,y), 'resize' (w,h)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'list', 'focus', 'minimize', 'close', 'move', 'resize'",
                    "enum": ["list", "focus", "minimize", "close", "move", "resize"],
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID (required for all actions except 'list')",
                },
                "x": {"type": "integer", "description": "X position for 'move' action"},
                "y": {"type": "integer", "description": "Y position for 'move' action"},
                "width": {"type": "integer", "description": "Width for 'resize' action"},
                "height": {"type": "integer", "description": "Height for 'resize' action"},
            },
            "required": ["action"],
        },
    ),
    Tool(
        name="context",
        description=(
            "Get a quick context snapshot: frontmost app, active window, focused element, "
            "and a summary of interactive elements. One call instead of multiple tools. "
            "Use this to orient yourself before interacting with an app. "
            "Set fast=True for a lightweight snapshot (app+window+focus only, no tree traversal)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fast": {
                    "type": "boolean",
                    "description": (
                        "If true, return only app name, window title, and focused element "
                        "(skips full tree traversal — much faster)."
                    ),
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="shadow",
        description=(
            "Execute browser actions in the background WITHOUT focusing Chrome. "
            "Actions: 'click' (by text/selector), 'type' (into focused/selected element), "
            "'press_key' (Enter/Tab/Escape/etc), 'scroll' (up/down), "
            "'read' (get all interactive elements), 'js' (raw JavaScript). "
            "Works on any Chrome tab without stealing focus from your current app."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'click', 'type', 'press_key', 'scroll', 'read', 'js'",
                    "enum": ["click", "type", "press_key", "scroll", "read", "js"],
                },
                "text": {
                    "type": "string",
                    "description": "Text to click (for 'click'), text to type (for 'type'), key name (for 'press_key'), JS code (for 'js')",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector (optional, for targeted click/type/scroll)",
                },
                "direction": {
                    "type": "string",
                    "description": "Scroll direction: 'up' or 'down' (default 'down')",
                    "default": "down",
                },
                "amount": {
                    "type": "integer",
                    "description": "Scroll amount in pixels (default 300)",
                    "default": 300,
                },
                "tab_index": {
                    "type": "integer",
                    "description": "Chrome tab index (0-based, default: active tab)",
                    "default": -1,
                },
            },
            "required": ["action"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # Auto-setup on first tool call (first run or version upgrade only)
        setup_msg = _maybe_auto_setup()
        result = await _dispatch(name, arguments)
        if setup_msg:
            result = f"{setup_msg}\n\n---\n\n{result}"
        return [TextContent(type="text", text=result)]
    except Exception as e:
        import traceback
        logger.error("Tool '%s' failed: %s\n%s", name, e, traceback.format_exc())
        # Return generic message to avoid leaking internal paths/state to caller
        return [TextContent(type="text", text=f"ERROR: Tool '{name}' failed unexpectedly. Check server logs for details.")]


# Dispatch table — built lazily on first call so all handlers are defined.
_DISPATCH_TABLE: dict[str, object] | None = None


def _build_dispatch_table() -> dict[str, object]:
    """O(1) lookup instead of 30+ if/elif branches."""
    return {
        "status": lambda args: _handle_status(),
        "list_apps": lambda args: _handle_list_apps(),
        "tree": _handle_get_tree,
        "find": _handle_find,
        "click": _handle_click,
        "type": _handle_type,
        "focused": lambda args: _handle_get_focused(),
        "list_tabs": lambda args: _handle_list_chrome_tabs(),
        "web_tree": _handle_get_web_tree,
        "navigate": _handle_navigate,
        "js": _handle_evaluate,
        "press_key": _handle_press_key,
        "wait": _handle_wait_for,
        "new_tab": _handle_new_tab,
        "close_tab": _handle_close_tab,
        "dialog": _handle_dialog,
        "upload": _handle_file_upload,
        "scroll": _handle_scroll,
        "drag": _handle_drag,
        "fill_form": _handle_fill_form,
        "hover": _handle_hover,
        "app": _handle_app,
        "subtree": _handle_get_subtree,
        "window": _handle_window,
        "context": _handle_context,
        "shadow": _handle_shadow,
    }


async def _dispatch(name: str, args: dict) -> str:
    global _DISPATCH_TABLE
    if _DISPATCH_TABLE is None:
        _DISPATCH_TABLE = _build_dispatch_table()
    handler = _DISPATCH_TABLE.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    result = handler(args)
    if asyncio.iscoroutine(result):
        return await result
    return result


# ── Native handlers ─────────────────────────────────────────────────
def _handle_status() -> str:
    chrome_binary = _pu.get_chrome_binary()
    launch_cmd = _pu.get_chrome_launch_cmd()
    discovered_port = _pu.discover_cdp_port()
    input_backend = _input_backend

    # Determine active tier for status report
    if cdp_pool.is_connected:
        tier_manager.set_available(ConnectionTier.CDP, True)
    active_tier = tier_manager.best_tier()
    tier_label = {
        ConnectionTier.CDP: "Tier 1 — Persistent CDP WebSocket",
        ConnectionTier.NATIVE: "Tier 2 — Native AX + AppleScript (CDP not connected)",
    }.get(active_tier, str(active_tier))

    cdp_available = tier_manager.is_available(ConnectionTier.CDP)

    lines = [
        "=== agent-eyes status ===",
        f"Platform: {sys.platform}",
        _platform_status(),
        "",
        f"Active tier: {tier_label}",
        "",
        "Connection tiers:",
        f"  Tier 1 — CDP Persistent Connection: {'connected' if cdp_available else 'not connected'}"
        f" (port {cdp_pool.active_port}, {len(cdp_pool.list_tabs())} tab(s) tracked)",
        "  Tier 2 — Native Fallback: available (always)",
        "",
        f"Input backend: {input_backend.__class__.__name__} "
        f"({'available' if input_backend.is_available() else 'NOT available'})",
        f"Chrome binary: {chrome_binary or 'not found'}",
        f"CDP auto-discovered port: {discovered_port or 'none (DevToolsActivePort not found)'}",
        f"CDP launch command: {launch_cmd}",
    ]

    if sys.platform == "darwin" and _as is not None:
        lines.append(f"AppleScript fallback: {'available' if _as.is_available() else 'unavailable (Chrome not running?)'}")
    elif sys.platform != "darwin":
        lines.append("AppleScript fallback: N/A (macOS only) — CDP required for browser tabs")

    # First-run auto-setup is now handled globally in call_tool()

    return "\n".join(lines)


def _handle_list_apps() -> str:
    if not native_adapter:
        return "ERROR: No native adapter available. Install platform dependencies."
    apps = native_adapter.list_apps()
    if not apps:
        return "No applications with visible windows found."

    lines = ["PID    | Name                          | Windows"]
    lines.append("-" * 70)
    for app in apps:
        front = " *" if app.is_frontmost else ""
        wins = ", ".join(app.windows[:3]) or "(no windows)"
        if len(app.windows) > 3:
            wins += f" (+{len(app.windows) - 3} more)"
        lines.append(f"{app.pid:<6} | {app.name[:30]:<30}{front} | {wins}")

    lines.append(f"\n{len(apps)} apps found. * = frontmost")
    lines.append("Use tree with a PID to see the app's UI.")
    return "\n".join(lines)


async def _handle_get_tree(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."
    pid = args.get("pid")
    if pid is None:
        return "ERROR: pid is required."

    is_browser = _pu.is_browser_pid(pid)

    # Universal depth: default 10, max 20.
    max_depth = min(args.get("max_depth", 10), 20)

    # Build tree — all adapters accept is_browser
    # Run in executor to avoid blocking the asyncio event loop
    loop = asyncio.get_running_loop()
    tree = await loop.run_in_executor(
        None, lambda: native_adapter.get_tree(pid, max_depth, is_browser=is_browser)
    )

    if tree is None:
        return f"ERROR: Could not get accessibility tree for PID {pid}. App may not be running or permission denied."

    # Auto-retry: if web content found but tree is sparse, rebuild deeper
    has_web, interactive_count = _analyze_tree(tree)

    if has_web and interactive_count < 5 and max_depth < 20:
        # Web content exists but not enough interactive elements reached.
        # Retry with max depth to capture deeply nested buttons/inputs.
        tree = await loop.run_in_executor(
            None, lambda: native_adapter.get_tree(pid, 20, is_browser=is_browser)
        )
        if tree is None:
            return f"ERROR: Could not rebuild accessibility tree for PID {pid}."
        _, interactive_count = _analyze_tree(tree)
        max_depth = 20

    registry.register_tree(tree, pid=pid)
    text = tree.to_text(max_depth=max_depth)

    # Metadata and advisories
    meta = f"Accessibility tree for PID {pid} ({registry.count()} elements"
    if has_web:
        meta += ", web content detected"
    meta += "):"

    advisory = ""
    if has_web and interactive_count < 5:
        advisory = (
            "\n\n── Web content not yet visible in native tree ──────────────\n"
            "The app may not have built its accessibility tree yet.\n"
            "Try again — agent-eyes has signaled the app to enable accessibility.\n"
            "If this persists, try: tree with max_depth=20"
        )
    elif interactive_count < 3 and registry.count() > 5:
        advisory = (
            "\n\nNote: few interactive elements found. "
            "Try web_tree for web content or increase max_depth."
        )

    return (
        f"{meta}\n\n"
        f"{text}{advisory}\n\n"
        f"Use [id] numbers with click or type to interact."
    )


_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textfield", "textarea", "combobox",
    "checkbox", "radiobutton", "slider", "menuitem", "tab",
    "searchfield", "popupbutton", "switch", "togglebutton",
})

_WEB_ROLES = frozenset({"webarea", "web area", "document"})


def _analyze_tree(element) -> tuple[bool, int]:
    """Analyze tree in a single O(n) pass. Returns (has_web_content, interactive_count)."""
    has_web = element.role in _WEB_ROLES
    count = 1 if element.role in _INTERACTIVE_ROLES else 0
    for child in element.children:
        child_web, child_count = _analyze_tree(child)
        has_web = has_web or child_web
        count += child_count
    return has_web, count


def _count_interactive(element) -> int:
    """Count interactive elements in the tree."""
    return _analyze_tree(element)[1]


def _match_text(query: str, text: str, match_type: str = "contains") -> bool:
    """Match text using the specified strategy."""
    if not query:
        return True
    text_lower = text.lower()
    query_lower = query.lower()
    if match_type == "exact":
        return text_lower == query_lower
    elif match_type == "prefix":
        return text_lower.startswith(query_lower)
    elif match_type == "suffix":
        return text_lower.endswith(query_lower)
    elif match_type == "regex":
        import re
        if len(query) > 500:
            return False
        try:
            return bool(re.search(query, text, re.IGNORECASE))
        except re.error:
            return False
    else:  # contains (default)
        return query_lower in text_lower


def _handle_find(args: dict) -> str:
    pid = args.get("pid")
    role = args.get("role", "")
    name = args.get("name", "")
    value = args.get("value", "")
    match_type = args.get("match", "contains")

    if not role and not name and not value:
        return "ERROR: Specify at least one of: role, name, value"

    if pid and native_adapter:
        elements = native_adapter.find_elements(pid, role, name, value)
        # Re-register found elements
        registry.register_elements(elements)
        # Apply match-type filtering consistently on role, name, and value
        if match_type != "contains":
            elements = [
                el for el in elements
                if (not role or _match_text(role, el.role, match_type))
                and (not name or _match_text(name, el.name, match_type))
                and (not value or _match_text(value, el.value, match_type))
            ]
    else:
        # Filter from registry using _match_text (consistent for all fields)
        all_elements = list(registry._elements.values())
        elements = []
        for el in all_elements:
            if role and not _match_text(role, el.role, match_type):
                continue
            if name and not _match_text(name, el.name, match_type):
                continue
            if value and not _match_text(value, el.value, match_type):
                continue
            if role or name or value:
                elements.append(el)

    if not elements:
        return "No matching elements found."

    lines = [f"Found {len(elements)} matching element(s):\n"]
    for el in elements[:20]:
        lines.append(el.to_text(max_depth=1))
    if len(elements) > 20:
        lines.append(f"\n... and {len(elements) - 20} more")
    return "\n".join(lines)


async def _verify_focus(pid: int, timeout: float = 0.5, retries: int = 3) -> tuple[bool, str]:
    """Verify app is frontmost after activation. Returns (success, error_message).

    This prevents the race condition where another app steals focus between
    activate_window() and the actual click/type action.
    """
    input_backend = _input_backend
    if not input_backend.is_available():
        return True, ""  # Can't verify, assume success

    delay = timeout / retries

    for attempt in range(retries):
        if input_backend.is_frontmost(pid):
            return True, ""
        if attempt < retries - 1:
            await asyncio.sleep(delay)
            # Re-activate in case something stole focus
            input_backend.activate_window(pid)

    return False, f"Could not bring app (PID {pid}) to front after {retries} attempts"


async def _handle_click(args: dict) -> str:
    element_id = args.get("id")
    click_x = args.get("x")
    click_y = args.get("y")
    click_pid = args.get("pid")

    # Coordinate-based click (from OCR hints or manual)
    if click_x is not None and click_y is not None:
        input_backend = _input_backend
        if not input_backend.is_available():
            return "ERROR: No input backend available for coordinate click."
        if click_pid:
            input_backend.activate_window(click_pid)
            await asyncio.sleep(0.1)
        if input_backend.click(click_x, click_y):
            return f"Clicked at ({click_x}, {click_y})"
        return f"ERROR: Could not click at ({click_x}, {click_y})."

    if element_id is None:
        return "ERROR: id is required (or provide x, y coordinates)."

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found. Call tree first."

    # Validate element reference is still alive (app may have navigated, window closed)
    if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
        if not native_adapter.is_element_valid(element):
            return f"ERROR: Element [{element_id}] is stale (UI has changed). Call tree to refresh."

    # Route CDP elements to CDP backend (unified: works for both stealth and existing browser)
    if element.source == "cdp" and element.platform_ref is not None:
        if not _cached_tabs:
            err = await _ensure_tabs()
            if err:
                return err
        if _cached_tabs:
            # Use element's tab_index, not hardcoded 0
            tab_idx = element.tab_index if element.tab_index >= 0 else 0
            if tab_idx >= len(_cached_tabs):
                return f"ERROR: Element's tab (index {tab_idx}) no longer exists. Call web_tree to refresh."
            tab = _cached_tabs[tab_idx]
            # Validate CDP element is still valid (not stale from page changes)
            if not await cdp_client.is_element_valid(tab, element.platform_ref):
                return f"ERROR: Element [{element_id}] is stale (page has changed). Call web_tree to refresh."
            success = await cdp_client.click_element(tab, element.platform_ref)
            if success:
                return f"Clicked [{element_id}] {element.role} \"{element.name}\" (tab {tab_idx})"
            return f"ERROR: Could not click [{element_id}] via CDP."

    # Native path for non-CDP elements
    if not native_adapter:
        return "ERROR: No native adapter available."

    # Strategy 1: AX action (reliable for most UI elements)
    for action in ("press", "click", "confirm", "open"):
        if native_adapter.perform_action(element, action):
            return f"Clicked [{element_id}] {element.role} \"{element.name}\""

    # Strategy 2: Coordinate-based click (human-like fallback)
    # Activate the correct app first so the click goes to the right window.
    if element.bounds:
        input_backend = _input_backend
        if input_backend.is_available():
            if element.pid:
                input_backend.activate_window(element.pid)
                # Verify focus before clicking (prevents race condition)
                focus_ok, focus_err = await _verify_focus(element.pid)
                if not focus_ok:
                    return f"ERROR: {focus_err}. Click aborted to prevent wrong target."
            x, y, w, h = element.bounds
            cx, cy = x + w // 2, y + h // 2
            if input_backend.click(cx, cy):
                return (
                    f"Clicked [{element_id}] {element.role} \"{element.name}\" "
                    f"(coordinate click at {cx},{cy})"
                )

    return f"ERROR: Could not click [{element_id}]. Available actions: {element.actions}"


async def _handle_type(args: dict) -> str:
    element_id = args.get("id")
    text = args.get("text", "")
    if element_id is None:
        return "ERROR: id is required."

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found. Call tree first."

    # Validate element reference is still alive (app may have navigated, window closed)
    if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
        if not native_adapter.is_element_valid(element):
            return f"ERROR: Element [{element_id}] is stale (UI has changed). Call tree to refresh."

    # Route CDP elements to CDP backend — try Tier 2 (persistent) first, then Tier 3 (legacy)
    if element.source == "cdp" and element.platform_ref is not None:
        tab_idx = element.tab_index if element.tab_index >= 0 else 0

        # ── Tier 2: persistent CDP session (single WebSocket, Input.insertText) ──
        session, tier2_tab, tier2_err = await _get_cdp_session({"tab_index": tab_idx})
        if session is not None:
            try:
                await session.enable_domain("DOM")
                await session.enable_domain("Runtime")
                # Focus the element
                result = await session.send(
                    "DOM.resolveNode",
                    {"backendNodeId": element.platform_ref},
                )
                object_id = result.get("object", {}).get("objectId")
                if object_id:
                    await session.send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": "function() { this.focus(); }",
                            "objectId": object_id,
                        },
                    )
                    # Insert full text in one CDP call (vs 2N calls for per-char dispatch)
                    await session.send("Input.insertText", {"text": text})
                    # Verify typing worked
                    await asyncio.sleep(0.1)
                    val_result = await session.send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": "function() { return this.value || this.textContent || ''; }",
                            "objectId": object_id,
                            "returnByValue": True,
                        },
                    )
                    actual_value = val_result.get("result", {}).get("value")
                    if actual_value is not None and text in str(actual_value):
                        return f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" (tab {tab_idx}, verified)"
                    elif actual_value is not None:
                        return (
                            f"WARNING: Typed \"{text}\" but verification failed. "
                            f"Current value: \"{str(actual_value)[:80]}\". "
                            f"The element may not accept input or may have transformed it."
                        )
                    return f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" (tab {tab_idx}, unverified)"
            except Exception as exc:
                logger.debug("_handle_type: Tier 2 failed: %s", exc)
                # Fall through to Tier 3

        # ── Tier 3: legacy per-request CDP (fallback) ──
        if not _cached_tabs:
            err = await _ensure_tabs()
            if err:
                return err
        if _cached_tabs:
            if tab_idx >= len(_cached_tabs):
                return f"ERROR: Element's tab (index {tab_idx}) no longer exists. Call web_tree to refresh."
            tab = _cached_tabs[tab_idx]
            if not await cdp_client.is_element_valid(tab, element.platform_ref):
                return f"ERROR: Element [{element_id}] is stale (page has changed). Call web_tree to refresh."
            success = await cdp_client.type_text(tab, element.platform_ref, text)
            if success:
                await asyncio.sleep(0.1)
                actual_value = await cdp_client.get_element_value(tab, element.platform_ref)
                if actual_value is not None and text in actual_value:
                    return f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" (tab {tab_idx}, verified)"
                elif actual_value is not None:
                    return (
                        f"WARNING: Typed \"{text}\" but verification failed. "
                        f"Current value: \"{actual_value[:80]}{'...' if len(actual_value) > 80 else ''}\". "
                        f"The element may not accept input or may have transformed it."
                    )
                else:
                    return f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" (tab {tab_idx}, unverified)"
            return f"ERROR: Could not type into [{element_id}] via CDP."

    # Native path for non-CDP elements
    if not native_adapter:
        return "ERROR: No native adapter available."

    input_backend = _input_backend
    is_web = "scrolltovisible" in element.actions  # web elements have this action

    # ── Step 1: Activate the target app window (always, for all strategies)
    if element.pid and input_backend.is_available():
        input_backend.activate_window(element.pid)
        # Verify focus before typing (prevents race condition)
        focus_ok, focus_err = await _verify_focus(element.pid)
        if not focus_ok:
            return f"ERROR: {focus_err}. Type aborted to prevent wrong target."

    # ── Secure text field detection (NSSecureTextField / SecureField)
    # These fields call EnableSecureEventInput() when focused, which blocks ALL
    # CGEvent keyboard injection at the HID level. set_value is the only option.
    is_secure = "secure" in element.states

    # ── Strategy 1: Focus + keyboard injection (primary — triggers keyDown events)
    # This is how screen readers type. Real keystrokes trigger ALL event handlers:
    # keyDown, keyUp, textDidChange, input, change — works for both native and web.
    # SKIP for secure text fields — CGEvent will always fail, wastes 0.25s.
    keyboard_injected = False
    if not is_secure and hasattr(native_adapter, 'focus_element') and input_backend.is_available():
        if native_adapter.focus_element(element):
            await asyncio.sleep(0.1)
            # For web elements: type directly WITHOUT clear_and_type.
            # clear_and_type sends Cmd+A + Delete before typing, which in Chrome
            # can select the entire page, trigger shortcuts (Cmd+T = new tab), and
            # cause havoc when the contentEditable div doesn't have perfect focus.
            if is_web:
                type_ok = input_backend.type_text(text)
            else:
                type_ok = input_backend.clear_and_type(text)
            if type_ok:
                # For web elements: trust keyboard injection, skip verification.
                if is_web:
                    return (
                        f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" "
                        f"(focus + keyboard injection)"
                    )

                # For native elements: verify the text actually landed.
                # Some apps (e.g. Jamf, secure input) silently ignore CGEvent
                # keystrokes while AX set_value works.
                await asyncio.sleep(0.15)
                verified = False
                if element.platform_ref and hasattr(native_adapter, '_read_attr'):
                    current_val = native_adapter._read_attr(element.platform_ref, "AXValue")
                    if current_val and text in str(current_val):
                        verified = True
                    elif current_val is None:
                        # Can't verify (field doesn't expose value) — assume success
                        verified = True
                else:
                    verified = True  # No way to verify, assume success

                if verified:
                    return (
                        f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" "
                        f"(focus + keyboard injection)"
                    )
                # Keyboard injection didn't land — mark it and fall through to set_value
                keyboard_injected = True

    # ── Strategy 2: Coordinate click + type (when focus_element fails)
    # Skip if keyboard injection already tried and failed (same mechanism, same result)
    if not keyboard_injected and input_backend.is_available() and element.bounds:
        x, y, w, h = element.bounds
        cx, cy = x + w // 2, y + h // 2
        if input_backend.click_and_type(cx, cy, text):
            # Verify text landed
            await asyncio.sleep(0.15)
            verified = False
            if element.platform_ref and hasattr(native_adapter, '_read_attr'):
                current_val = native_adapter._read_attr(element.platform_ref, "AXValue")
                if current_val and text in str(current_val):
                    verified = True
                elif current_val is None:
                    verified = True
            else:
                verified = True

            if verified:
                return (
                    f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" "
                    f"(coordinate click + type)"
                )

    # ── Strategy 3: set_value + AXConfirm (fallback for apps that block keyboard injection)
    # Used when keyboard injection fails verification, OR when no input backend available.
    # Works for Jamf Connect, security apps, and apps with secure/custom text fields.
    if native_adapter.set_value(element, text):
        if element.platform_ref:
            native_adapter.perform_action(element, "confirm")
        method = "set_value fallback — keyboard injection didn't land" if keyboard_injected else "set_value"
        return (
            f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\" "
            f"({method})"
        )

    return f"ERROR: Could not type into [{element_id}]. Element may not be editable."


def _handle_get_focused() -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    element = native_adapter.get_focused_element()
    if element is None:
        return "No focused element found."

    registry.register_element(element)
    return f"Focused element:\n{element.to_text(max_depth=2)}"


# ── CDP handlers ────────────────────────────────────────────────────
# _cached_tabs_time removed — Tier 2 (cdp_pool) tracks tabs via auto-attach;
# Tier 3 (_ensure_tabs) uses its own 30-second cache window internally.
_cached_tabs: list = []
_tabs_lock = asyncio.Lock()  # Protects _cached_tabs from concurrent mutation


async def _get_cdp_session(args: dict) -> tuple:
    """Try to get the best available CDP session for the requested tab.

    Tier fallback chain:
      Tier 1 — Persistent single-WebSocket CDP (cdp_pool): needs
               --remote-debugging-port.
      Tier 2 — Legacy per-request CDP / native AX fallback (cdp_client):
               always attempted last.

    Returns:
        (session_or_None, tab_or_None, error_string)
        - If Tier 1 succeeds: (CDPSession, ChromeTab, "")
        - If Tier 1 fails and Tier 2 available: (None, ChromeTab, "")
        - If both fail: (None, None, error_string)
    """
    tab_index = args.get("tab_index", 0)

    # ── Tier 1: persistent WebSocket ──
    try:
        await cdp_pool.ensure_connected()
        tabs = cdp_pool.list_tabs()
        if tabs and tab_index < len(tabs):
            session = cdp_pool.get_session_for_tab(tab_index)
            if session is not None:
                tier_manager.set_available(ConnectionTier.CDP, True)
                return session, tabs[tab_index], ""
    except Exception as exc:
        logger.debug("_get_cdp_session: Tier 2 unavailable: %s", exc)

    # ── Tier 2 fallback: legacy cdp_client ──
    # _ensure_tabs / cdp_client are still used for all other handlers
    err = await _ensure_tabs()
    if not err:
        tab, err = _get_tab(args)
        if not err:
            return None, tab, ""
        return None, None, err

    return None, None, err


async def _handle_list_chrome_tabs() -> str:
    global _cached_tabs

    # Always refresh when explicitly listing tabs
    err = await _ensure_tabs(force=True)
    if err and not _cached_tabs:
        pass  # fall through to the non-CDP paths below

    # Try CDP first (richer interaction, cross-platform)
    # Prefer Tier 2 (cdp_pool) — zero network calls when connected
    if cdp_pool.is_connected:
        pool_tabs = cdp_pool.list_tabs()
        if pool_tabs:
            async with _tabs_lock:
                _cached_tabs = list(pool_tabs)
            lines = ["Chrome tabs (via persistent CDP):\n"]
            for i, tab in enumerate(pool_tabs):
                lines.append(f"[{i}] {tab.title}")
                lines.append(f"    {tab.url}\n")
            lines.append("Use web_tree with tab_index to see a tab's UI.")
            return "\n".join(lines)

    available = await cdp_client.is_available()
    if available:
        tabs = await cdp_client.list_tabs()
        async with _tabs_lock:
            _cached_tabs = list(tabs)

        if not tabs:
            return "Chrome is running but no tabs found."

        port_info = f" (port {cdp_client.active_port})"
        lines = [f"Chrome tabs (via CDP{port_info}):\n"]
        for i, tab in enumerate(tabs):
            lines.append(f"[{i}] {tab.title}")
            lines.append(f"    {tab.url}\n")

        lines.append("Use web_tree with tab_index to see a tab's UI.")
        return "\n".join(lines)

    # Fallback: AppleScript (macOS only, no --remote-debugging-port needed)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        as_tabs = _as.list_chrome_tabs()
        if as_tabs:
            launch_cmd = _pu.get_chrome_launch_cmd()
            lines = ["Chrome tabs (via AppleScript — CDP not available):\n"]
            for tab in as_tabs:
                lines.append(f"[{tab.index}] {tab.title}")
                lines.append(f"    {tab.url}  (window {tab.window_index})\n")

            lines.append(
                "Note: For full web tree interaction (web_tree, click),\n"
                f"start Chrome with: {launch_cmd}\n\n"
                "Current mode supports: tab listing, page content reading via tree on Chrome PID."
            )
            return "\n".join(lines)

    # No CDP, no AppleScript — show platform-appropriate instructions
    launch_cmd = _pu.get_chrome_launch_cmd()
    return (
        "Chrome remote debugging not available.\n\n"
        f"Start Chrome with CDP enabled:\n  {launch_cmd}\n\n"
        "Tip: agent-eyes can auto-discover the CDP port if Chrome writes a\n"
        "DevToolsActivePort file. Use --remote-debugging-port=0 for an auto-assigned port."
    )


_chrome_pid_cache: int = 0


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is still running."""
    try:
        import os
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_chrome_pid() -> int:
    """Find Chrome's process ID for window activation.

    Returns the PID of Chrome (or Chromium), or 0 if not found.
    Uses a cache with staleness guard to avoid repeated subprocess spawns.
    """
    global _chrome_pid_cache
    if _chrome_pid_cache and _is_pid_alive(_chrome_pid_cache):
        return _chrome_pid_cache

    try:
        import subprocess
        # Try common Chrome process names
        for browser in ["Google Chrome", "Chromium", "Chrome"]:
            result = subprocess.run(
                ["pgrep", "-x", browser],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = int(result.stdout.strip().split()[0])
                _chrome_pid_cache = pid
                return pid
        # Fallback: search by pattern
        result = subprocess.run(
            ["pgrep", "-f", "Google Chrome"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split()[0])
            _chrome_pid_cache = pid
            return pid
    except Exception:
        pass
    _chrome_pid_cache = 0
    return 0


async def _handle_get_web_tree(args: dict) -> str:
    tab_index = args.get("tab_index", 0)
    max_depth = min(args.get("max_depth", 5), 10)

    # Try Tier 2 (persistent CDP) then Tier 3 (legacy CDP)
    session, tab, cdp_err = await _get_cdp_session(args)

    if session is not None and tab is not None:
        # Tier 2: get accessibility tree via persistent session
        try:
            await session.enable_domain("Accessibility")
            result = await session.send(
                "Accessibility.getFullAXTree", {"depth": max_depth}
            )
            nodes = result.get("nodes", [])
            if nodes:
                # Build tree using the legacy CDPClient helper (reuse parsing logic)
                tree = cdp_client._build_tree(nodes)
                if tree is not None:
                    chrome_pid = _get_chrome_pid()
                    registry.register_tree(tree, pid=chrome_pid, tab_index=tab_index)
                    text = tree.to_text(max_depth=max_depth)
                    return (
                        f"Web accessibility tree for: {tab.title}\n"
                        f"URL: {tab.url}\n"
                        f"Elements: {registry.count()}\n\n"
                        f"{text}\n\n"
                        f"Use [id] numbers with click or type to interact."
                    )
        except Exception as exc:
            logger.debug("_handle_get_web_tree: Tier 2 failed: %s", exc)
            # Fall through to Tier 3

    # Tier 3: legacy cdp_client (per-request WebSocket)
    if tab is None:
        # Auto-fetch tabs via legacy CDP if not yet available
        available = await cdp_client.is_available()
        if available:
            tabs = await cdp_client.list_tabs()
            async with _tabs_lock:
                _cached_tabs.extend(tabs)

    if _cached_tabs:
        if tab_index >= len(_cached_tabs):
            return f"ERROR: Tab index {tab_index} out of range. Only {len(_cached_tabs)} tabs available."

        legacy_tab = _cached_tabs[tab_index]
        tree = await cdp_client.get_accessibility_tree(legacy_tab, max_depth)
        if tree is not None:
            chrome_pid = _get_chrome_pid()
            registry.register_tree(tree, pid=chrome_pid, tab_index=tab_index)  # Track which tab elements came from
            text = tree.to_text(max_depth=max_depth)
            return (
                f"Web accessibility tree for: {legacy_tab.title}\n"
                f"URL: {legacy_tab.url}\n"
                f"Elements: {registry.count()}\n\n"
                f"{text}\n\n"
                f"Use [id] numbers with click or type to interact."
            )

    # Fallback: AppleScript JS injection (macOS only, no CDP required)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        script = build_ax_tree_script(max_depth=max_depth)
        raw = _as.execute_javascript(script, tab_index=tab_index)
        if raw:
            try:
                import json as _json
                tree_dict = _json.loads(raw)
                text = format_ax_tree(tree_dict)
                # Get tab info for context
                as_tabs = _as.list_chrome_tabs()
                tab_title = as_tabs[tab_index].title if as_tabs and tab_index < len(as_tabs) else "unknown"
                tab_url = as_tabs[tab_index].url if as_tabs and tab_index < len(as_tabs) else "unknown"
                return (
                    f"Web accessibility tree for: {tab_title}\n"
                    f"URL: {tab_url}\n"
                    "(via AppleScript JS injection — CDP not available)\n\n"
                    f"{text}\n\n"
                    "Note: element [id]s are JS-generated — use shadow for interaction without CDP."
                )
            except Exception as e:
                logger.debug("AppleScript web tree parse failed: %s", e)

    return (
        "ERROR: This feature requires Chrome Extension (Tier 1) or CDP (Tier 2). "
        "Start Chrome with: --remote-debugging-port=9222"
    )


# ── CDP action handlers ─────────────────────────────────────────────

async def _ensure_tabs(force: bool = False) -> str:
    """Ensure cached tabs are available. Returns error string or empty.

    NOTE: This is the Tier 3 fallback only. Tier 2 (cdp_pool / PersistentCDP)
    tracks tabs automatically via Target.attachedToTarget and does not use
    this function. Only handlers that have not yet been migrated to _get_cdp_session
    should call _ensure_tabs directly.

    Args:
        force: Always refresh, ignoring cache age. Use when listing tabs explicitly.
    """
    global _cached_tabs
    async with _tabs_lock:
        if not force and _cached_tabs:
            return ""
        available = await cdp_client.is_available()
        if not available:
            return "ERROR: Chrome remote debugging not available. Start Chrome with --remote-debugging-port=9222"
        tabs = await cdp_client.list_tabs()
        _cached_tabs = list(tabs)
        if not _cached_tabs:
            return "ERROR: No Chrome tabs found."
        return ""


def _get_tab(args: dict) -> tuple:
    """Get tab by index from args. Returns (tab, error_string)."""
    idx = args.get("tab_index", 0)
    if not isinstance(idx, int) or idx < 0 or idx >= len(_cached_tabs):
        return None, f"ERROR: Tab index {idx} out of range. {len(_cached_tabs)} tab(s) available."
    return _cached_tabs[idx], ""


_SAFE_URL_SCHEMES = frozenset({"http", "https", "about", "chrome", "chrome-extension"})


def _validate_url(url: str) -> str | None:
    """Validate URL scheme. Returns error string or None if valid."""
    import urllib.parse
    if url.startswith("--"):
        return "ERROR: URL must not start with '--' (flag injection)."
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        return "ERROR: URL must include an explicit scheme (e.g., https://)."
    if parsed.scheme not in _SAFE_URL_SCHEMES:
        return f"ERROR: URL scheme '{parsed.scheme}' is not permitted. Use http, https, or about:blank."
    return None


async def _handle_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        return "ERROR: url is required."

    url_err = _validate_url(url)
    if url_err:
        return url_err

    # Try Tier 2 (persistent CDP) then Tier 3 (legacy CDP)
    session, tab, cdp_err = await _get_cdp_session(args)

    if tab is not None:
        if session is not None:
            # Tier 2: persistent WebSocket path
            try:
                await session.send("Page.navigate", {"url": url})
                # Give the page a moment to start loading
                await asyncio.sleep(0.3)
                result = await session.send("Target.getTargetInfo", {"targetId": tab.id})
                target_info = result.get("targetInfo", {})
                return (
                    f"Navigated to: {target_info.get('url', url)}\n"
                    f"Title: {target_info.get('title', '(loading)')}"
                )
            except Exception as exc:
                logger.debug("_handle_navigate: Tier 2 failed: %s", exc)
                # Fall through to Tier 3

        # Tier 3: legacy cdp_client
        result = await cdp_client.navigate(tab, url)
        if "error" in result:
            return f"ERROR: Navigation failed: {result['error']}"
        return f"Navigated to: {result.get('url', url)}\nTitle: {result.get('title', '(loading)')}"

    # Fallback: AppleScript (macOS only — can navigate existing tabs)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        tab_index = args.get("tab_index", 0)
        result = _as.navigate_tab(url, tab_index=tab_index)
        if not result.startswith("ERROR"):
            return result + "\n(navigated via AppleScript — CDP not available)"
        # AppleScript failed — fall through to CLI

    # Fallback: cross-platform CLI (opens new tab — cannot navigate existing)
    success, msg = _pu.open_url_in_browser(url)
    if success:
        return (
            f"{msg}\n"
            "Note: Opened in a new tab (navigating existing tabs requires CDP).\n"
            "(via platform CLI — CDP not available)"
        )

    launch_cmd = _pu.get_chrome_launch_cmd()
    return (
        f"ERROR: Cannot navigate — {msg}\n\n"
        f"Start Chrome with CDP enabled:\n  {launch_cmd}"
    )


async def _handle_evaluate(args: dict) -> str:
    expression = args.get("expression")
    if not expression:
        return "ERROR: expression is required."

    cdp_error = None

    # Try Tier 2 (persistent CDP) then Tier 3 (legacy CDP)
    session, tab, tier_err = await _get_cdp_session(args)

    if session is not None:
        # Tier 2: evaluate via persistent session
        try:
            await session.enable_domain("Runtime")
            result = await session.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            exc_details = result.get("exceptionDetails")
            if exc_details:
                cdp_error = exc_details.get("text", "runtime exception")
            else:
                value = result.get("result", {}).get("value")
                if value is None:
                    return "Result: undefined"
                return f"Result: {json.dumps(value, indent=2, default=str) if not isinstance(value, str) else value}"
        except Exception as exc:
            logger.debug("_handle_evaluate: Tier 2 failed: %s", exc)
            cdp_error = str(exc)

    elif tab is not None:
        # Tier 3: legacy cdp_client
        result = await cdp_client.evaluate(tab, expression)
        if "error" not in result:
            value = result.get("value")
            if value is None:
                return "Result: undefined"
            return f"Result: {json.dumps(value, indent=2, default=str) if not isinstance(value, str) else value}"
        cdp_error = result.get("error", "unknown")
    else:
        cdp_error = tier_err

    # Fallback: AppleScript JS execution (macOS, no CDP needed)
    if _as is not None:
        try:
            tab_index = args.get("tab_index", 0)
            result = _as.execute_javascript(expression, tab_index=tab_index)
            if result is not None:
                return f"Result: {result}"
            return "Result: undefined (AppleScript JS — async/Promises not supported)"
        except Exception as e:
            return f"ERROR: AppleScript JS failed: {e}"

    if cdp_error:
        return f"ERROR: {cdp_error}"
    return "ERROR: No JavaScript execution method available. Enable CDP or use macOS."


async def _handle_press_key(args: dict) -> str:
    key = args.get("key")
    if not key:
        return "ERROR: key is required."

    pid = args.get("pid")
    modifiers = args.get("modifiers", [])
    mod_str = "+".join(modifiers) + "+" if modifiers else ""

    # ── Normalize key names to input_sim format ──
    key_map = {
        "enter": "return", "arrowup": "up", "arrowdown": "down",
        "arrowleft": "left", "arrowright": "right",
        "backspace": "delete", "pageup": "page_up", "pagedown": "page_down",
    }
    native_key = key_map.get(key.lower(), key.lower())

    # ── Normalize modifier names for input_sim ──
    mod_map = {"ctrl": "control", "meta": "command", "cmd": "command"}

    # ── Route: if PID given, check if it's a native app or browser ──
    if pid is not None:
        is_browser = _pu.is_browser_pid(pid)

        if not is_browser:
            # Native app path — use OS-level input simulation
            input_backend = _input_backend
            if not input_backend.is_available():
                return "ERROR: No input backend available for native key press."

            # Activate target and verify it's frontmost before sending keys.
            # This prevents keys like Escape/Cmd+A from hitting the wrong app
            # (e.g., Claude Code's terminal) due to activation race conditions.
            input_backend.activate_window(pid)
            await asyncio.sleep(0.1)
            if not input_backend.is_frontmost(pid):
                # Retry with longer delay
                input_backend.activate_window(pid)
                await asyncio.sleep(0.3)
                if not input_backend.is_frontmost(pid):
                    return (
                        f"ERROR: Could not bring app (PID {pid}) to front. "
                        f"Key '{key}' NOT sent to avoid hitting the wrong app."
                    )

            if modifiers:
                native_mods = [mod_map.get(m.lower(), m.lower()) for m in modifiers]
                hotkey_keys = native_mods + [native_key]
                success = input_backend.hotkey(*hotkey_keys)
            else:
                success = input_backend.press_key(native_key)

            if success:
                return f"Pressed {mod_str}{key} in native app (PID {pid})"
            return f"ERROR: Could not press key '{key}' in native app (PID {pid})."

    # ── Chrome/web path — use CDP ──
    err = await _ensure_tabs()
    if err:
        # Fallback: if no Chrome tabs but we have an input backend, use native
        input_backend = _input_backend
        if input_backend.is_available():
            if modifiers:
                native_mods = [mod_map.get(m.lower(), m.lower()) for m in modifiers]
                hotkey_keys = native_mods + [native_key]
                success = input_backend.hotkey(*hotkey_keys)
            else:
                success = input_backend.press_key(native_key)
            if success:
                return f"Pressed {mod_str}{key} (native input fallback — no Chrome tabs)"
        return err

    tab, err = _get_tab(args)
    if err:
        return err

    success = await cdp_client.press_key(tab, key, modifiers)
    if success:
        return f"Pressed {mod_str}{key} in Chrome tab"
    return f"ERROR: Could not press key '{key}' via CDP."


_MAX_WAIT_TIMEOUT = 60.0  # seconds — cap to prevent MCP server DoS


async def _handle_wait_for(args: dict) -> str:
    role = args.get("role", "")
    name = args.get("name", "")
    timeout = min(float(args.get("timeout", 5.0)), _MAX_WAIT_TIMEOUT)

    if not role and not name:
        return "ERROR: Specify at least one of: role, name"

    pid = args.get("pid")
    if pid is not None and native_adapter:
        # Native app polling — check for element appearance
        start = time.time()
        loop = asyncio.get_running_loop()
        while time.time() - start < timeout:
            if role or name:
                elements = await loop.run_in_executor(
                    None, lambda: native_adapter.find_elements(pid, role=role, name=name)
                )
                if elements:
                    # Register found elements
                    registry.register_elements(elements)
                    el = elements[0]
                    return (
                        f"Found [{el.id}] {el.role} \"{el.name}\" "
                        f"after {time.time() - start:.1f}s"
                    )
            await asyncio.sleep(0.5)
        return f"Timeout after {timeout}s: no element matching role='{role}' name='{name}' found."

    err = await _ensure_tabs()
    if not err:
        tab, err = _get_tab(args)
        if not err:
            element = await cdp_client.wait_for_element(tab, role, name, timeout)
            if element:
                registry.register_element(element)
                return (
                    f"Found element: [{element.id}] {element.role} \"{element.name}\"\n"
                    f"Use this [id] with click or type."
                )
            return f"Timeout: element not found after {timeout}s (role={role!r}, name={name!r})"

    # Fallback: AppleScript shadow_read_interactive polling (macOS only)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        tab_index = args.get("tab_index", 0)
        start = time.time()
        while time.time() - start < timeout:
            content = _as.shadow_read_interactive(tab_index=tab_index)
            if content:
                role_match = not role or role.lower() in content.lower()
                name_match = not name or name.lower() in content.lower()
                if role_match and name_match:
                    return (
                        f"Found element matching role={role!r} name={name!r} "
                        f"after {time.time() - start:.1f}s "
                        "(via AppleScript polling — CDP not available)\n"
                        f"Content snippet:\n{content[:500]}"
                    )
            await asyncio.sleep(0.5)
        return f"Timeout: element not found after {timeout}s (role={role!r}, name={name!r})"

    return f"Timeout: element not found after {timeout}s (role={role!r}, name={name!r})"


async def _handle_new_tab(args: dict) -> str:
    global _cached_tabs

    url = args.get("url", "about:blank")

    # Try CDP first
    available = await cdp_client.is_available()
    if available:
        if not _cached_tabs:
            tabs = await cdp_client.list_tabs()
            async with _tabs_lock:
                _cached_tabs.extend(tabs)
        tab = await cdp_client.new_tab(url)
        if tab is None:
            return "ERROR: Could not create new tab."
        async with _tabs_lock:
            _cached_tabs.append(tab)
            idx = len(_cached_tabs) - 1
        return f"New tab [{idx}]: {tab.title}\nURL: {tab.url}"

    # Fallback: AppleScript (macOS only — richer interaction)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        tab = _as.open_new_tab(url)
        if tab is not None:
            return (
                f"New tab [{tab.index}]: {tab.title}\n"
                f"URL: {tab.url}\n"
                "(opened via AppleScript — CDP not available)"
            )

    # Fallback: cross-platform CLI browser open (macOS/Windows/Linux)
    success, msg = _pu.open_url_in_browser(url)
    if success:
        return f"{msg}\n(opened via platform CLI — CDP not available)"

    launch_cmd = _pu.get_chrome_launch_cmd()
    return (
        f"ERROR: Cannot open new tab — {msg}\n\n"
        f"Start Chrome with CDP enabled:\n  {launch_cmd}"
    )


async def _handle_close_tab(args: dict) -> str:
    global _cached_tabs

    # Always force-refresh tabs for destructive operations
    err = await _ensure_tabs(force=True)
    if err:
        # Fallback: AppleScript close tab (macOS only)
        if sys.platform == "darwin" and _as is not None and _as.is_available():
            title_query = args.get("title")
            idx = args.get("tab_index", 0)

            # Find tab index by title if provided
            if title_query:
                as_tabs = _as.list_chrome_tabs()
                query_lower = title_query.lower()
                matches = [(t.index, t) for t in as_tabs if query_lower in t.title.lower()]
                if not matches:
                    tab_list = "\n".join(f"  [{t.index}] {t.title}" for t in as_tabs)
                    return f"ERROR: No tab matching '{title_query}'. Open tabs:\n{tab_list}"
                if len(matches) > 1:
                    match_list = "\n".join(f"  [{t.index}] {t.title}" for _, t in matches)
                    return (
                        f"ERROR: Multiple tabs match '{title_query}'. "
                        f"Specify tab_index:\n{match_list}"
                    )
                idx, matched_tab = matches[0]

            # Validate idx is a non-negative integer before AppleScript interpolation
            if not isinstance(idx, int) or idx < 0:
                return f"ERROR: Invalid tab index: {idx!r}"

            # Close via AppleScript
            close_script = f'''
            tell application "Google Chrome"
                close tab {int(idx) + 1} of window 1
            end tell
            '''
            import subprocess as _sp
            result = _sp.run(
                ["osascript", "-e", close_script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return (
                    f"Closed tab [{idx}] (via AppleScript — CDP not available)"
                )
            return f"ERROR: AppleScript close tab failed: {result.stderr.strip()}"

        return err

    title_query = args.get("title")
    idx = args.get("tab_index")

    # If title is provided, find the matching tab by title (case-insensitive substring)
    if title_query:
        query_lower = title_query.lower()
        matches = [
            (i, t) for i, t in enumerate(_cached_tabs)
            if query_lower in t.title.lower()
        ]
        if not matches:
            tab_list = "\n".join(
                f"  [{i}] {t.title} — {t.url}" for i, t in enumerate(_cached_tabs)
            )
            return f"ERROR: No tab matching '{title_query}'. Open tabs:\n{tab_list}"
        if len(matches) > 1:
            match_list = "\n".join(
                f"  [{i}] {t.title} — {t.url}" for i, t in matches
            )
            return (
                f"ERROR: Multiple tabs match '{title_query}'. "
                f"Specify tab_index to close the correct one:\n{match_list}"
            )
        idx, tab = matches[0]
    else:
        # Default to index-based lookup
        if idx is None:
            idx = 0
        tab, tab_err = _get_tab({"tab_index": idx})
        if tab_err:
            return tab_err

    # Include tab details in confirmation for transparency
    tab_title = getattr(tab, "title", "unknown")
    tab_url = getattr(tab, "url", "unknown")
    success = await cdp_client.close_tab(tab)
    if success:
        async with _tabs_lock:
            if idx < len(_cached_tabs):
                _cached_tabs.pop(idx)
        return f"Closed tab [{idx}]: {tab_title}\n  URL: {tab_url}"
    return f"ERROR: Could not close tab [{idx}]: {tab_title} — {tab_url}"


async def _handle_dialog(args: dict) -> str:
    err = await _ensure_tabs()
    if not err:
        tab, err = _get_tab(args)
        if not err:
            accept = args.get("accept", True)
            prompt_text = args.get("prompt_text", "")
            success = await cdp_client.handle_dialog(tab, accept, prompt_text)
            if success:
                action = "Accepted" if accept else "Dismissed"
                return f"{action} dialog."
            return "ERROR: No dialog to handle or dialog handling failed."

    # Limited fallback: JavaScript dialogs block the page event loop so AppleScript
    # cannot interact with them. CDP is required for reliable dialog handling.
    return (
        "ERROR: Handling JavaScript dialogs requires CDP (Chrome DevTools Protocol). "
        "Start Chrome with --remote-debugging-port=9222 to use this feature."
    )


async def _handle_file_upload(args: dict) -> str:
    import os

    element_id = args.get("id")
    files = args.get("files", [])
    if element_id is None:
        return "ERROR: id is required."
    if not files:
        return "ERROR: files list is required."

    # Validate all file paths exist and are safe to upload
    import pathlib
    _home = pathlib.Path.home()
    _blocked_prefixes = [
        _home / ".ssh",
        _home / ".aws",
        _home / ".gnupg",
        _home / ".config" / "gcloud",
        _home / ".config" / "op",
        _home / ".kube",
        _home / ".docker",
        _home / ".netrc",
        _home / ".npmrc",
        _home / ".pypirc",
        _home / ".gem" / "credentials",
        pathlib.Path("/etc"),
    ]
    validated: list[str] = []
    for path in files:
        # Resolve symlinks before checking to prevent symlink bypass
        abs_path = os.path.realpath(os.path.abspath(path))
        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path!r}"
        abs_p = pathlib.Path(abs_path)
        for blocked in _blocked_prefixes:
            if abs_p.is_relative_to(blocked):
                return f"ERROR: Upload of files from {blocked} is not permitted for security."
        validated.append(abs_path)

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found."
    if element.source != "cdp" or element.platform_ref is None:
        return (
            "ERROR: File upload requires CDP (Chrome DevTools Protocol). "
            "Start Chrome with --remote-debugging-port=9222 to use this feature."
        )

    err = await _ensure_tabs()
    if not err:
        tab, err = _get_tab(args)
        if not err:
            success = await cdp_client.set_file_input(tab, element.platform_ref, validated)
            if success:
                return f"Uploaded {len(validated)} file(s) to [{element_id}]."
            return "ERROR: File upload failed."

    # Limited fallback: DOM.setFileInputFiles requires CDP — no AppleScript equivalent.
    return (
        "ERROR: File upload requires CDP (Chrome DevTools Protocol). "
        "Start Chrome with --remote-debugging-port=9222 to use this feature."
    )


async def _handle_scroll(args: dict) -> str:
    x = args.get("x", 400)
    y = args.get("y", 400)
    delta_x = args.get("delta_x", 0)
    delta_y = args.get("delta_y", 300)
    pid = args.get("pid")

    # ── Native path: use OS-level scroll events for non-browser apps
    if pid is not None:
        input_backend = _input_backend
        if input_backend.is_available():
            # Convert from CDP convention (positive=down) to CGEvent convention (negative=down)
            native_delta_y = -delta_y if delta_y != 0 else 0
            native_delta_x = -delta_x if delta_x != 0 else 0
            success = input_backend.scroll(x, y, delta_x=native_delta_x, delta_y=native_delta_y)
            if success:
                direction = "down" if delta_y > 0 else "up" if delta_y < 0 else ""
                if delta_x:
                    direction += (" + right" if delta_x > 0 else " + left")
                return f"Scrolled {direction} by ({delta_x}, {delta_y}) in native app (pid={pid})"
        return "ERROR: Native scroll failed or no input backend available."

    # ── CDP path: scroll in browser tab
    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    success = await cdp_client.scroll(tab, x, y, delta_x, delta_y)
    if success:
        direction = "down" if delta_y > 0 else "up" if delta_y < 0 else ""
        if delta_x:
            direction += (" + right" if delta_x > 0 else " + left")
        return f"Scrolled {direction} by ({delta_x}, {delta_y})"
    return "ERROR: Scroll failed."


async def _handle_drag(args: dict) -> str:
    for field in ("from_x", "from_y", "to_x", "to_y"):
        if args.get(field) is None:
            return f"ERROR: {field} is required."

    from_x = args["from_x"]
    from_y = args["from_y"]
    to_x = args["to_x"]
    to_y = args["to_y"]

    err = await _ensure_tabs()
    if not err:
        tab, err = _get_tab(args)
        if not err:
            success = await cdp_client.drag(tab, from_x, from_y, to_x, to_y)
            if success:
                return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
            return "ERROR: Drag failed."

    # Fallback: OS input simulation (works for native apps and browser windows)
    input_backend = _input_backend
    if input_backend.is_available() and hasattr(input_backend, "drag"):
        success = input_backend.drag(from_x, from_y, to_x, to_y)
        if success:
            return (
                f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y}) "
                "(via OS input simulation — CDP not available)"
            )

    return (
        "ERROR: This feature requires Chrome Extension (Tier 1) or CDP (Tier 2). "
        "Start Chrome with: --remote-debugging-port=9222"
    )


async def _handle_fill_form(args: dict) -> str:
    fields = args.get("fields", [])
    if not fields:
        return "ERROR: fields list is required."

    err = await _ensure_tabs()
    if not err:
        filled = []
        errors = []
        for field in fields:
            eid = field.get("id")
            value = field.get("value", "")
            element = registry.get(eid)
            if element is None:
                errors.append(f"[{eid}] not found")
                continue
            if element.source != "cdp" or element.platform_ref is None:
                errors.append(f"[{eid}] not a web element")
                continue
            # Use element's tab_index, not hardcoded 0
            tab_idx = element.tab_index if element.tab_index >= 0 else 0
            if tab_idx >= len(_cached_tabs):
                errors.append(f"[{eid}] tab {tab_idx} no longer exists")
                continue
            tab = _cached_tabs[tab_idx]
            # Validate CDP element is still valid
            if not await cdp_client.is_element_valid(tab, element.platform_ref):
                errors.append(f"[{eid}] is stale (page changed)")
                continue
            success = await cdp_client.type_text(tab, element.platform_ref, value)
            if success:
                filled.append(f"[{eid}] {element.role} \"{element.name}\" = \"{value}\"")
            else:
                errors.append(f"[{eid}] type failed")

        parts = []
        if filled:
            parts.append(f"Filled {len(filled)} field(s):\n" + "\n".join(f"  {f}" for f in filled))
        if errors:
            parts.append(f"Errors:\n" + "\n".join(f"  {e}" for e in errors))
        return "\n".join(parts) or "No fields processed."

    # Fallback: AppleScript shadow_type per field (macOS only)
    if sys.platform == "darwin" and _as is not None and _as.is_available():
        tab_index = args.get("tab_index", 0)
        filled = []
        errors = []
        for field in fields:
            eid = field.get("id")
            value = field.get("value", "")
            ok = _as.shadow_type(value, tab_index=tab_index)
            if ok:
                filled.append(f"[{eid}] = \"{value}\"")
            else:
                errors.append(f"[{eid}] type failed")

        parts = []
        if filled:
            parts.append(
                f"Filled {len(filled)} field(s) (via AppleScript — CDP not available):\n"
                + "\n".join(f"  {f}" for f in filled)
            )
        if errors:
            parts.append("Errors:\n" + "\n".join(f"  {e}" for e in errors))
        return "\n".join(parts) or "No fields processed."

    return (
        "ERROR: This feature requires Chrome Extension (Tier 1) or CDP (Tier 2). "
        "Start Chrome with: --remote-debugging-port=9222"
    )


# ── New tool handlers ────────────────────────────────────────────────

async def _handle_hover(args: dict) -> str:
    hover_x = args.get("x")
    hover_y = args.get("y")
    element_id = args.get("id")

    if hover_x is not None and hover_y is not None:
        if _input_backend.is_available():
            _input_backend.move_mouse(hover_x, hover_y)
            return f"Hovering at ({hover_x}, {hover_y})"
        return "ERROR: No input backend available."

    if element_id is None:
        return "ERROR: id or (x, y) is required."

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found. Call tree first."

    if element.bounds:
        x, y, w, h = element.bounds
        cx, cy = x + w // 2, y + h // 2
        if _input_backend.is_available():
            if element.pid:
                _input_backend.activate_window(element.pid)
                await asyncio.sleep(0.1)
            _input_backend.move_mouse(cx, cy)
            return f"Hovering over [{element_id}] {element.role} \"{element.name}\" at ({cx}, {cy})"

    return f"ERROR: Element [{element_id}] has no bounds for hover."


def _handle_app(args: dict) -> str:
    action = args.get("action", "").lower()
    name = args.get("name", "")
    if not action or not name:
        return "ERROR: action and name are required."

    if sys.platform != "darwin":
        return "ERROR: app currently only supports macOS."

    try:
        from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration
        ws = NSWorkspace.sharedWorkspace()
    except ImportError:
        return "ERROR: AppKit not available."

    if action == "launch":
        # Try as bundle ID first, then as app name
        if "." in name:
            success = ws.launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_(
                name, 0, None, None
            )
            if success[0]:
                return f"Launched app with bundle ID '{name}'."
        # Try by name
        success = ws.launchApplication_(name)
        if success:
            return f"Launched '{name}'."
        return f"ERROR: Could not launch '{name}'. Check the app name or bundle ID."

    elif action == "focus":
        for app in ws.runningApplications():
            app_name = app.localizedName() or ""
            bundle_id = app.bundleIdentifier() or ""
            if name.lower() in app_name.lower() or name.lower() == bundle_id.lower():
                app.activateWithOptions_(0)
                return f"Focused '{app_name}' (PID {app.processIdentifier()})."
        return f"ERROR: App '{name}' not found running."

    elif action == "quit":
        for app in ws.runningApplications():
            app_name = app.localizedName() or ""
            bundle_id = app.bundleIdentifier() or ""
            if name.lower() in app_name.lower() or name.lower() == bundle_id.lower():
                app.terminate()
                return f"Quit '{app_name}'."
        return f"ERROR: App '{name}' not found running."

    return f"ERROR: Unknown action '{action}'. Use 'launch', 'quit', or 'focus'."


def _handle_get_subtree(args: dict) -> str:
    element_id = args.get("id")
    max_depth = min(args.get("max_depth", 5), 15)
    if element_id is None:
        return "ERROR: id is required."

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found. Call tree first."

    if not native_adapter:
        return "ERROR: No native adapter available."

    # Validate element is still alive
    if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
        if not native_adapter.is_element_valid(element):
            return f"ERROR: Element [{element_id}] is stale. Call tree to refresh."

    # Re-traverse from this element's platform_ref
    if element.platform_ref is None:
        return f"ERROR: Element [{element_id}] has no native reference for subtree expansion."

    subtree = native_adapter._element_to_ui(element.platform_ref, 0, max_depth)
    if subtree is None:
        return f"ERROR: Could not expand subtree for [{element_id}]."

    registry.register_tree(subtree, pid=element.pid)
    text = subtree.to_text(max_depth=max_depth)

    return (
        f"Subtree of [{element_id}] ({_count_interactive(subtree)} interactive elements):\n\n"
        f"{text}\n\n"
        f"Use [id] numbers with click or type to interact."
    )


def _handle_window(args: dict) -> str:
    action = args.get("action", "").lower()
    pid = args.get("pid")

    if action == "list":
        if sys.platform != "darwin":
            return "ERROR: Window listing currently supports macOS only."
        try:
            import Quartz
            window_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            lines = ["Windows on screen:\n"]
            for win in window_list:
                layer = win.get(Quartz.kCGWindowLayer, 999)
                if layer != 0:
                    continue
                w_pid = win.get(Quartz.kCGWindowOwnerPID, 0)
                w_name = win.get(Quartz.kCGWindowOwnerName, "")
                w_title = win.get(Quartz.kCGWindowName, "")
                bounds = win.get(Quartz.kCGWindowBounds, {})
                x = int(bounds.get("X", 0))
                y = int(bounds.get("Y", 0))
                w = int(bounds.get("Width", 0))
                h = int(bounds.get("Height", 0))
                lines.append(f"  PID {w_pid} | {w_name} | \"{w_title}\" | pos=({x},{y}) size={w}x{h}")
            return "\n".join(lines) if len(lines) > 1 else "No windows found."
        except ImportError:
            return "ERROR: Quartz not available."

    if pid is None:
        return "ERROR: pid is required for this action."
    if not native_adapter:
        return "ERROR: No native adapter available."

    # Get the app's window via AX
    try:
        from ApplicationServices import AXUIElementCreateApplication, AXValueCreate, kAXValueCGPointType, kAXValueCGSizeType
        from Quartz import CGPoint, CGSize
        ax_app = native_adapter._ax.AXUIElementCreateApplication(pid)
        window = native_adapter._read_attr(ax_app, "AXFocusedWindow")
        if window is None:
            windows = native_adapter._read_attr(ax_app, "AXWindows")
            if windows and len(windows) > 0:
                window = windows[0]
        if window is None:
            return f"ERROR: No window found for PID {pid}."
    except Exception as e:
        return f"ERROR: Could not access window: {e}"

    if action == "focus":
        try:
            native_adapter._ax.AXUIElementPerformAction(window, "AXRaise")
            input_backend = _input_backend
            if input_backend.is_available():
                input_backend.activate_window(pid)
            return f"Focused window for PID {pid}."
        except Exception as e:
            return f"ERROR: {e}"

    elif action == "minimize":
        try:
            native_adapter._ax.AXUIElementSetAttributeValue(window, "AXMinimized", True)
            return f"Minimized window for PID {pid}."
        except Exception as e:
            return f"ERROR: {e}"

    elif action == "close":
        try:
            close_button = native_adapter._read_attr(window, "AXCloseButton")
            if close_button:
                native_adapter._ax.AXUIElementPerformAction(close_button, "AXPress")
                return f"Closed window for PID {pid}."
            return "ERROR: No close button found."
        except Exception as e:
            return f"ERROR: {e}"

    elif action == "move":
        x = args.get("x")
        y = args.get("y")
        if x is None or y is None:
            return "ERROR: x and y are required for move."
        try:
            new_pos = AXValueCreate(kAXValueCGPointType, CGPoint(float(x), float(y)))
            native_adapter._ax.AXUIElementSetAttributeValue(window, "AXPosition", new_pos)
            return f"Moved window to ({x}, {y})."
        except Exception as e:
            return f"ERROR: {e}"

    elif action == "resize":
        width = args.get("width")
        height = args.get("height")
        if width is None or height is None:
            return "ERROR: width and height are required for resize."
        try:
            new_size = AXValueCreate(kAXValueCGSizeType, CGSize(float(width), float(height)))
            native_adapter._ax.AXUIElementSetAttributeValue(window, "AXSize", new_size)
            return f"Resized window to {width}x{height}."
        except Exception as e:
            return f"ERROR: {e}"

    return f"ERROR: Unknown action '{action}'."


def _handle_context(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    fast = args.get("fast", False)

    # Fast mode: return only app name + window title + focused element.
    # Skip full tree traversal for speed.
    if fast:
        apps = native_adapter.list_apps()
        frontmost = next((a for a in apps if a.is_frontmost), None)

        lines = []
        if frontmost:
            lines.append(f"Frontmost app: {frontmost.name} (PID {frontmost.pid})")
            if frontmost.windows:
                lines.append(f"Active window: \"{frontmost.windows[0]}\"")
            focused = native_adapter.get_focused_element()
            if focused:
                registry.register_element(focused)
                lines.append(f"Focused: [{focused.id}] {focused.role} \"{focused.name}\"")
        else:
            lines.append("No frontmost app detected.")
        return "\n".join(lines)

    lines = []

    # Get frontmost app
    apps = native_adapter.list_apps()
    frontmost = None
    for app in apps:
        if app.is_frontmost:
            frontmost = app
            break

    if frontmost:
        lines.append(f"Frontmost app: {frontmost.name} (PID {frontmost.pid})")
        if frontmost.windows:
            lines.append(f"Active window: \"{frontmost.windows[0]}\"")

        # Get focused element
        focused = native_adapter.get_focused_element()
        if focused:
            registry.register_element(focused)
            lines.append(f"Focused: [{focused.id}] {focused.role} \"{focused.name}\"")

        # Get tree and count interactive elements
        tree = native_adapter.get_tree(frontmost.pid, max_depth=10, is_browser=False)
        if tree:
            registry.register_tree(tree, pid=frontmost.pid)
            has_web, interactive = _analyze_tree(tree)
            lines.append(f"Elements: {registry.count()} total, {interactive} interactive")
            if has_web:
                lines.append("Web content detected (Electron/browser/webview)")

            # List interactive elements (compact)
            lines.append("\nInteractive elements:")

            def collect_interactive(el, results, max_items=30):
                if len(results) >= max_items:
                    return
                if el.role in _INTERACTIVE_ROLES:
                    results.append(el)
                for child in el.children:
                    collect_interactive(child, results, max_items)

            interactive_els = []
            collect_interactive(tree, interactive_els)
            for el in interactive_els:
                state = " ".join(el.states) if el.states else ""
                lines.append(f"  [{el.id}] {el.role} \"{el.name}\" {state}".rstrip())
            if interactive > len(interactive_els):
                lines.append(f"  ... and {interactive - len(interactive_els)} more")
    else:
        lines.append("No frontmost app detected.")
        apps_summary = ", ".join(f"{a.name} ({a.pid})" for a in apps[:5])
        lines.append(f"Running apps: {apps_summary}")

    return "\n".join(lines)


# ── Shadow (background browser) handler ─────────────────────────────
def _handle_shadow(args: dict) -> str:
    action = args.get("action", "")
    text = args.get("text", "")
    selector = args.get("selector", "")
    tab_idx = args.get("tab_index", -1)
    direction = args.get("direction", "down")
    amount = args.get("amount", 300)

    if sys.platform != "darwin":
        return "ERROR: Shadow mode currently supports macOS only."

    from . import applescript as _as
    if not _as.is_available():
        return "ERROR: Chrome is not running."

    # Resolve tab index
    if tab_idx < 0:
        tab_idx = _as.shadow_get_active_tab_index() or 0

    if action == "click":
        if not text and not selector:
            return "ERROR: 'text' or 'selector' required for click."
        if selector:
            ok = _as.shadow_click(selector, tab_index=tab_idx)
            return f"Shadow clicked '{selector}'" if ok else f"ERROR: Element '{selector}' not found."
        else:
            result = _as.shadow_click_by_text(text, tab_index=tab_idx)
            return f"Shadow clicked: {result}" if result else f"ERROR: No clickable element with text '{text}' found."

    elif action == "type":
        if not text:
            return "ERROR: 'text' required for type."
        ok = _as.shadow_type(text, selector=selector, tab_index=tab_idx)
        return f"Shadow typed \"{text}\"" if ok else "ERROR: Could not type in background."

    elif action == "press_key":
        key = text or "Enter"
        ok = _as.shadow_press_key(key, tab_index=tab_idx)
        return f"Shadow pressed {key}" if ok else f"ERROR: Could not press {key}."

    elif action == "scroll":
        ok = _as.shadow_scroll(direction=direction, amount=amount, selector=selector, tab_index=tab_idx)
        return f"Shadow scrolled {direction} {amount}px" if ok else "ERROR: Could not scroll."

    elif action == "read":
        result = _as.shadow_read_interactive(tab_index=tab_idx)
        if result:
            return f"Interactive elements (background scan):\n\n{result}"
        return "No interactive elements found or Chrome not available."

    elif action == "js":
        if not text:
            return "ERROR: 'text' (JS code) required for js action."
        result = _as.shadow_execute_js(text, tab_index=tab_idx)
        if result is not None:
            return f"JS result: {result}"
        return "ERROR: JavaScript execution failed."

    return f"ERROR: Unknown action '{action}'."


# ── Entry point ─────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO)
    logger.info("agent-eyes starting — platform: %s", sys.platform)

    if native_adapter:
        ok, msg = native_adapter.check_permissions()
        logger.info("Native adapter: %s — %s", native_adapter.__class__.__name__, msg)
    else:
        logger.warning("No native accessibility adapter available")

    asyncio.run(_run())


async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    main()
