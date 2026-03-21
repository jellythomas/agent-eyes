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
from .registry import ElementRegistry
from . import platform_utils as _pu
from .input_sim import get_input_backend

# AppleScript is macOS-only — import conditionally
if sys.platform == "darwin":
    from . import applescript as _as
else:
    _as = None  # type: ignore

logger = logging.getLogger("agent-eyes")

# ── Platform detection ──────────────────────────────────────────────
def _get_native_adapter() -> BaseAdapter | None:
    """Auto-detect and return the platform adapter."""
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


# ── Server setup ────────────────────────────────────────────────────
app = Server("agent-eyes")
registry = ElementRegistry()
native_adapter = _get_native_adapter()
cdp_client = CDPClient()


def _platform_status() -> str:
    parts = []
    if native_adapter:
        ok, msg = native_adapter.check_permissions()
        parts.append(f"Native adapter: {native_adapter.__class__.__name__} — {msg}")
    else:
        parts.append("Native adapter: NOT AVAILABLE (missing dependencies)")
    parts.append("CDP (Chrome): check with eyes_list_chrome_tabs")
    return "\n".join(parts)


# ── Tool definitions ────────────────────────────────────────────────
TOOLS = [
    Tool(
        name="eyes_status",
        description=(
            "Check agent-eyes status: platform adapter, permissions, CDP availability. "
            "Call this first to verify the server is working."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="eyes_list_apps",
        description=(
            "List all running applications with visible windows. "
            "Returns PID, name, bundle ID, window titles. "
            "Use the PID to call eyes_get_tree."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="eyes_get_tree",
        description=(
            "Get the accessibility tree of an application by PID. "
            "Returns a numbered text representation of ALL UI elements — "
            "buttons, text fields, headings, tables, etc. "
            "Each element has an [id] you can use with eyes_click/eyes_type. "
            "This is the PRIMARY way to 'see' an application — no screenshot needed. "
            "For Chrome/Chromium browsers, automatically includes web page content "
            "(headings, buttons, inputs, links, chat items) via AppleScript on macOS "
            "or CDP on all platforms (macOS/Linux/Windows)."
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
        name="eyes_find",
        description=(
            "Search for UI elements by role, name, or value within an app. "
            "Searches the currently loaded tree (call eyes_get_tree first) "
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
            },
            "required": [],
        },
    ),
    Tool(
        name="eyes_click",
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
        name="eyes_type",
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
        name="eyes_get_focused",
        description=(
            "Get the currently focused UI element across all apps. "
            "Useful to see what's active without knowing which app/PID."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="eyes_list_chrome_tabs",
        description=(
            "List all Chrome browser tabs. Returns tab ID, title, URL. "
            "Uses CDP (Chrome DevTools Protocol) with auto-discovery of debug port. "
            "On macOS, falls back to AppleScript if CDP unavailable. "
            "On Linux/Windows, requires Chrome started with --remote-debugging-port. "
            "Use tab ID with eyes_get_web_tree for richer web content access."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="eyes_get_web_tree",
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
                    "description": "Tab index from eyes_list_chrome_tabs (0-based)",
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
        name="eyes_navigate",
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
        name="eyes_evaluate",
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
        name="eyes_press_key",
        description=(
            "Press a keyboard key in any application (native or web). "
            "For native apps, provide a PID to target that app. "
            "For Chrome/web, optionally provide tab_index. "
            "Supports special keys (Enter, Tab, Escape, Backspace, Delete, "
            "ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, F1-F12, Space) "
            "and modifiers (Ctrl, Alt, Meta/Cmd, Shift). "
            "For typing text into a field, use eyes_type instead."
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
        name="eyes_wait_for",
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
            },
            "required": [],
        },
    ),
    Tool(
        name="eyes_new_tab",
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
        name="eyes_close_tab",
        description="Close a Chrome tab by index.",
        inputSchema={
            "type": "object",
            "properties": {
                "tab_index": {
                    "type": "integer",
                    "description": "Tab index to close (default 0 = current)",
                    "default": 0,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="eyes_handle_dialog",
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
        name="eyes_file_upload",
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
        name="eyes_scroll",
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
        name="eyes_drag",
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
        name="eyes_fill_form",
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
        name="eyes_get_ocr_hints",
        description=(
            "Get visual text hints from a window screenshot using OCR. "
            "Use when the accessibility tree is insufficient (few interactive elements). "
            "Returns text blocks with screen coordinates for coordinate-based clicking. "
            "These are NOT semantic UI elements — text labels may or may not be interactive. "
            "Requires Screen Recording permission on macOS."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "description": "Process ID of the application",
                },
            },
            "required": ["pid"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=f"ERROR: {e}")]


async def _dispatch(name: str, args: dict) -> str:
    if name == "eyes_status":
        return _handle_status()
    elif name == "eyes_list_apps":
        return _handle_list_apps()
    elif name == "eyes_get_tree":
        return _handle_get_tree(args)
    elif name == "eyes_find":
        return _handle_find(args)
    elif name == "eyes_click":
        return await _handle_click(args)
    elif name == "eyes_type":
        return await _handle_type(args)
    elif name == "eyes_get_focused":
        return _handle_get_focused()
    elif name == "eyes_list_chrome_tabs":
        return await _handle_list_chrome_tabs()
    elif name == "eyes_get_web_tree":
        return await _handle_get_web_tree(args)
    elif name == "eyes_navigate":
        return await _handle_navigate(args)
    elif name == "eyes_evaluate":
        return await _handle_evaluate(args)
    elif name == "eyes_press_key":
        return await _handle_press_key(args)
    elif name == "eyes_wait_for":
        return await _handle_wait_for(args)
    elif name == "eyes_new_tab":
        return await _handle_new_tab(args)
    elif name == "eyes_close_tab":
        return await _handle_close_tab(args)
    elif name == "eyes_handle_dialog":
        return await _handle_dialog(args)
    elif name == "eyes_file_upload":
        return await _handle_file_upload(args)
    elif name == "eyes_scroll":
        return await _handle_scroll(args)
    elif name == "eyes_drag":
        return await _handle_drag(args)
    elif name == "eyes_fill_form":
        return await _handle_fill_form(args)
    elif name == "eyes_get_ocr_hints":
        return _handle_get_ocr_hints(args)
    else:
        return f"Unknown tool: {name}"


# ── Native handlers ─────────────────────────────────────────────────
def _handle_status() -> str:
    chrome_binary = _pu.get_chrome_binary()
    launch_cmd = _pu.get_chrome_launch_cmd()
    discovered_port = _pu.discover_cdp_port()
    input_backend = get_input_backend()

    lines = [
        "=== agent-eyes status ===",
        f"Platform: {sys.platform}",
        _platform_status(),
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
    lines.append("Use eyes_get_tree with a PID to see the app's UI.")
    return "\n".join(lines)


def _handle_get_tree(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."
    pid = args.get("pid")
    if pid is None:
        return "ERROR: pid is required."

    is_browser = _pu.is_browser_pid(pid)

    # Universal depth: default 10, max 20.
    max_depth = min(args.get("max_depth", 10), 20)

    # Build tree — pass is_browser for adapters that support it
    if hasattr(native_adapter, "get_tree"):
        import inspect
        sig = inspect.signature(native_adapter.get_tree)
        if "is_browser" in sig.parameters:
            tree = native_adapter.get_tree(pid, max_depth, is_browser=is_browser)
        else:
            tree = native_adapter.get_tree(pid, max_depth)
    else:
        tree = native_adapter.get_tree(pid, max_depth)

    if tree is None:
        return f"ERROR: Could not get accessibility tree for PID {pid}. App may not be running or permission denied."

    # Auto-retry: if web content found but tree is sparse, rebuild deeper
    has_web = _tree_has_web_content(tree)
    interactive_count = _count_interactive(tree)

    if has_web and interactive_count < 5 and max_depth < 20:
        # Web content exists but not enough interactive elements reached.
        # Retry with max depth to capture deeply nested buttons/inputs.
        if hasattr(native_adapter, "get_tree"):
            import inspect
            sig = inspect.signature(native_adapter.get_tree)
            if "is_browser" in sig.parameters:
                tree = native_adapter.get_tree(pid, 20, is_browser=is_browser)
            else:
                tree = native_adapter.get_tree(pid, 20)
        else:
            tree = native_adapter.get_tree(pid, 20)
        if tree is None:
            return f"ERROR: Could not rebuild accessibility tree for PID {pid}."
        interactive_count = _count_interactive(tree)
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
            "If this persists, try: eyes_get_tree with max_depth=20"
        )
    elif interactive_count < 3 and registry.count() > 5:
        advisory = (
            "\n\nNote: few interactive elements found. "
            "Use eyes_get_ocr_hints for visual text detection."
        )

    return (
        f"{meta}\n\n"
        f"{text}{advisory}\n\n"
        f"Use [id] numbers with eyes_click or eyes_type to interact."
    )


def _tree_has_web_content(element) -> bool:
    """Check if the native tree contains web area elements (reached web content)."""
    if element.role in ("webarea", "web area", "document"):
        return True
    for child in element.children:
        if _tree_has_web_content(child):
            return True
    return False


def _count_interactive(element, _interactive_roles=frozenset({
    "button", "link", "textfield", "textarea", "combobox",
    "checkbox", "radiobutton", "slider", "menuitem", "tab",
    "searchfield", "popupbutton", "switch", "togglebutton",
})) -> int:
    """Count interactive elements in the tree."""
    count = 1 if element.role in _interactive_roles else 0
    for child in element.children:
        count += _count_interactive(child)
    return count


def _handle_find(args: dict) -> str:
    pid = args.get("pid")
    role = args.get("role", "")
    name = args.get("name", "")
    value = args.get("value", "")

    if not role and not name and not value:
        return "ERROR: Specify at least one of: role, name, value"

    if pid and native_adapter:
        elements = native_adapter.find_elements(pid, role, name, value)
        # Re-register found elements
        for el in elements:
            registry._elements[el.id] = el
    else:
        elements = registry.find(role, name, value)

    if not elements:
        return "No matching elements found."

    lines = [f"Found {len(elements)} matching element(s):\n"]
    for el in elements[:20]:
        lines.append(el.to_text(max_depth=1))
    if len(elements) > 20:
        lines.append(f"\n... and {len(elements) - 20} more")
    return "\n".join(lines)


async def _handle_click(args: dict) -> str:
    element_id = args.get("id")
    click_x = args.get("x")
    click_y = args.get("y")
    click_pid = args.get("pid")

    # Coordinate-based click (from OCR hints or manual)
    if click_x is not None and click_y is not None:
        input_backend = get_input_backend()
        if not input_backend.is_available():
            return "ERROR: No input backend available for coordinate click."
        if click_pid:
            input_backend.activate_window(click_pid)
            time.sleep(0.1)
        if input_backend.click(click_x, click_y):
            return f"Clicked at ({click_x}, {click_y})"
        return f"ERROR: Could not click at ({click_x}, {click_y})."

    if element_id is None:
        return "ERROR: id is required (or provide x, y coordinates)."

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found. Call eyes_get_tree first."

    # Route CDP elements to CDP backend (unified: works for both stealth and existing browser)
    if element.source == "cdp" and element.platform_ref is not None:
        if not _cached_tabs:
            err = await _ensure_tabs()
            if err:
                return err
        if _cached_tabs:
            tab = _cached_tabs[0]
            success = await cdp_client.click_element(tab, element.platform_ref)
            if success:
                return f"Clicked [{element_id}] {element.role} \"{element.name}\""
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
        input_backend = get_input_backend()
        if input_backend.is_available():
            if element.pid:
                input_backend.activate_window(element.pid)
                import time as _time
                _time.sleep(0.1)
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
        return f"ERROR: Element [{element_id}] not found. Call eyes_get_tree first."

    # Route CDP elements to CDP backend (unified: works for both stealth and existing browser)
    if element.source == "cdp" and element.platform_ref is not None:
        if not _cached_tabs:
            err = await _ensure_tabs()
            if err:
                return err
        if _cached_tabs:
            tab = _cached_tabs[0]
            success = await cdp_client.type_text(tab, element.platform_ref, text)
            if success:
                return f"Typed \"{text}\" into [{element_id}] {element.role} \"{element.name}\""
            return f"ERROR: Could not type into [{element_id}] via CDP."

    # Native path for non-CDP elements
    if not native_adapter:
        return "ERROR: No native adapter available."

    input_backend = get_input_backend()
    is_web = "scrolltovisible" in element.actions  # web elements have this action

    # ── Step 1: Activate the target app window (always, for all strategies)
    if element.pid and input_backend.is_available():
        input_backend.activate_window(element.pid)
        time.sleep(0.1)

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
            time.sleep(0.1)
            if input_backend.clear_and_type(text):
                # Verify the text actually landed — some apps (e.g. Jamf, secure input)
                # silently ignore CGEvent keystrokes while AX set_value works.
                time.sleep(0.15)
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
            time.sleep(0.15)
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

    registry._elements[element.id] = element
    return f"Focused element:\n{element.to_text(max_depth=2)}"


# ── CDP handlers ────────────────────────────────────────────────────
_cached_tabs: list = []


async def _handle_list_chrome_tabs() -> str:
    global _cached_tabs

    # Try CDP first (richer interaction, cross-platform)
    available = await cdp_client.is_available()
    if available:
        tabs = await cdp_client.list_tabs()
        _cached_tabs = tabs

        if not tabs:
            return "Chrome is running but no tabs found."

        port_info = f" (port {cdp_client.active_port})"
        lines = [f"Chrome tabs (via CDP{port_info}):\n"]
        for i, tab in enumerate(tabs):
            lines.append(f"[{i}] {tab.title}")
            lines.append(f"    {tab.url}\n")

        lines.append("Use eyes_get_web_tree with tab_index to see a tab's UI.")
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
                "Note: For full web tree interaction (eyes_get_web_tree, eyes_click),\n"
                f"start Chrome with: {launch_cmd}\n\n"
                "Current mode supports: tab listing, page content reading via eyes_get_tree on Chrome PID."
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


async def _handle_get_web_tree(args: dict) -> str:
    tab_index = args.get("tab_index", 0)

    if not _cached_tabs:
        # Auto-fetch tabs
        available = await cdp_client.is_available()
        if not available:
            return "Chrome remote debugging not available. Start Chrome with --remote-debugging-port=9222"
        tabs = await cdp_client.list_tabs()
        _cached_tabs.extend(tabs)

    if tab_index >= len(_cached_tabs):
        return f"ERROR: Tab index {tab_index} out of range. Only {len(_cached_tabs)} tabs available."

    tab = _cached_tabs[tab_index]
    max_depth = min(args.get("max_depth", 5), 10)

    tree = await cdp_client.get_accessibility_tree(tab, max_depth)
    if tree is None:
        return f"ERROR: Could not get accessibility tree for tab '{tab.title}'"

    registry.register_tree(tree, pid=0)  # CDP elements don't have a native PID
    text = tree.to_text(max_depth=max_depth)

    return (
        f"Web accessibility tree for: {tab.title}\n"
        f"URL: {tab.url}\n"
        f"Elements: {registry.count()}\n\n"
        f"{text}\n\n"
        f"Use [id] numbers with eyes_click or eyes_type to interact."
    )


# ── CDP action handlers ─────────────────────────────────────────────

async def _ensure_tabs() -> str:
    """Ensure cached tabs are available. Returns error string or empty."""
    global _cached_tabs
    if not _cached_tabs:
        available = await cdp_client.is_available()
        if not available:
            return "ERROR: Chrome remote debugging not available. Start Chrome with --remote-debugging-port=9222"
        tabs = await cdp_client.list_tabs()
        _cached_tabs.extend(tabs)
    if not _cached_tabs:
        return "ERROR: No Chrome tabs found."
    return ""


def _get_tab(args: dict) -> tuple:
    """Get tab by index from args. Returns (tab, error_string)."""
    idx = args.get("tab_index", 0)
    if not isinstance(idx, int) or idx < 0 or idx >= len(_cached_tabs):
        return None, f"ERROR: Tab index {idx} out of range. {len(_cached_tabs)} tab(s) available."
    return _cached_tabs[idx], ""


async def _handle_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        return "ERROR: url is required."

    # Try CDP first
    available = await cdp_client.is_available()
    if available:
        err = await _ensure_tabs()
        if err:
            return err
        tab, err = _get_tab(args)
        if err:
            return err
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

    # Try CDP first
    cdp_error = None
    available = await cdp_client.is_available()
    if available:
        err = await _ensure_tabs()
        if not err:
            tab, err = _get_tab(args)
            if not err:
                result = await cdp_client.evaluate(tab, expression)
                if "error" not in result:
                    value = result.get("value")
                    if value is None:
                        return "Result: undefined"
                    return f"Result: {json.dumps(value, indent=2, default=str) if not isinstance(value, str) else value}"
                cdp_error = result.get("error", "unknown")
            else:
                cdp_error = err
        else:
            cdp_error = err

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
            input_backend = get_input_backend()
            if not input_backend.is_available():
                return "ERROR: No input backend available for native key press."

            input_backend.activate_window(pid)
            time.sleep(0.1)

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
        input_backend = get_input_backend()
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


async def _handle_wait_for(args: dict) -> str:
    role = args.get("role", "")
    name = args.get("name", "")
    timeout = args.get("timeout", 5.0)

    if not role and not name:
        return "ERROR: Specify at least one of: role, name"

    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    element = await cdp_client.wait_for_element(tab, role, name, timeout)
    if element:
        registry._elements[element.id] = element
        return (
            f"Found element: [{element.id}] {element.role} \"{element.name}\"\n"
            f"Use this [id] with eyes_click or eyes_type."
        )
    return f"Timeout: element not found after {timeout}s (role={role!r}, name={name!r})"


async def _handle_new_tab(args: dict) -> str:
    global _cached_tabs

    url = args.get("url", "about:blank")

    # Try CDP first
    available = await cdp_client.is_available()
    if available:
        if not _cached_tabs:
            tabs = await cdp_client.list_tabs()
            _cached_tabs.extend(tabs)
        tab = await cdp_client.new_tab(url)
        if tab is None:
            return "ERROR: Could not create new tab."
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

    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    idx = args.get("tab_index", 0)
    success = await cdp_client.close_tab(tab)
    if success:
        _cached_tabs.pop(idx)
        return f"Closed tab [{idx}]."
    return "ERROR: Could not close tab."


async def _handle_dialog(args: dict) -> str:
    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    accept = args.get("accept", True)
    prompt_text = args.get("prompt_text", "")
    success = await cdp_client.handle_dialog(tab, accept, prompt_text)
    if success:
        action = "Accepted" if accept else "Dismissed"
        return f"{action} dialog."
    return "ERROR: No dialog to handle or dialog handling failed."


async def _handle_file_upload(args: dict) -> str:
    import os

    element_id = args.get("id")
    files = args.get("files", [])
    if element_id is None:
        return "ERROR: id is required."
    if not files:
        return "ERROR: files list is required."

    # Validate all file paths exist before sending to CDP
    validated: list[str] = []
    for path in files:
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            return f"ERROR: File not found: {path!r}"
        validated.append(abs_path)

    element = registry.get(element_id)
    if element is None:
        return f"ERROR: Element [{element_id}] not found."
    if element.source != "cdp" or element.platform_ref is None:
        return f"ERROR: Element [{element_id}] is not a web element."

    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    success = await cdp_client.set_file_input(tab, element.platform_ref, validated)
    if success:
        return f"Uploaded {len(validated)} file(s) to [{element_id}]."
    return "ERROR: File upload failed."


async def _handle_scroll(args: dict) -> str:
    x = args.get("x", 400)
    y = args.get("y", 400)
    delta_x = args.get("delta_x", 0)
    delta_y = args.get("delta_y", 300)
    pid = args.get("pid")

    # ── Native path: use OS-level scroll events for non-browser apps
    if pid is not None:
        input_backend = get_input_backend()
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

    err = await _ensure_tabs()
    if err:
        return err
    tab, err = _get_tab(args)
    if err:
        return err

    success = await cdp_client.drag(
        tab, args["from_x"], args["from_y"], args["to_x"], args["to_y"]
    )
    if success:
        return (
            f"Dragged from ({args['from_x']}, {args['from_y']}) "
            f"to ({args['to_x']}, {args['to_y']})"
        )
    return "ERROR: Drag failed."


async def _handle_fill_form(args: dict) -> str:
    fields = args.get("fields", [])
    if not fields:
        return "ERROR: fields list is required."

    err = await _ensure_tabs()
    if err:
        return err
    tab = _cached_tabs[0]

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


def _handle_get_ocr_hints(args: dict) -> str:
    pid = args.get("pid")
    if pid is None:
        return "ERROR: pid is required."

    from .screenshot import capture_window
    from .ocr import get_ocr_engine

    engine = get_ocr_engine()
    if engine is None:
        import sys as _sys
        platform = _sys.platform
        if platform == "darwin":
            msg = "Install: pip install 'agent-eyes[macos]'"
        elif platform == "win32":
            msg = "Install: pip install winrt-Windows.Media.Ocr"
        else:
            msg = "Install: pip install pytesseract (+ apt install tesseract-ocr)"
        return f"ERROR: No OCR engine available. {msg}"

    capture = capture_window(pid)
    if capture is None:
        import sys as _sys
        if _sys.platform == "darwin":
            return "ERROR: Could not capture window. Check Screen Recording permission in System Settings > Privacy & Security."
        return f"ERROR: Could not capture window for PID {pid}."

    hints = engine.recognize(
        capture.image_data,
        scale_factor=capture.scale_factor,
        window_x=capture.window_x,
        window_y=capture.window_y,
        window_w=capture.window_w,
        window_h=capture.window_h,
    )

    if not hints:
        return "No text detected in window. The window may be empty or OCR could not read it."

    lines = [
        f"OCR text hints for PID {pid} ({len(hints)} text blocks detected).",
        "These are visual text positions — NOT semantic UI elements.",
        "Use eyes_click(x=..., y=..., pid=...) to click at these coordinates.",
        "",
    ]
    for h in sorted(hints, key=lambda h: (h.y, h.x)):
        cx = h.x + h.width // 2
        cy = h.y + h.height // 2
        lines.append(
            f'  "{h.text}" @({cx},{cy}) size={h.width}x{h.height} conf={h.confidence}'
        )

    return "\n".join(lines)


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
