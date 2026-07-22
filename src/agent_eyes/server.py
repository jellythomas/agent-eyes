"""Model-independent, native-first computer use over MCP.

Foreground access is cross-platform through macOS AXUIElement, Windows UI
Automation, and Linux AT-SPI2. Browser-remote protocols are optional explicit
shadow providers; they are never a prerequisite for visible computer use.
"""
from __future__ import annotations

import sys
import time
import asyncio
import logging
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, TextContent

from .adapters.base import BaseAdapter, UIElement, INTERACTIVE_ROLES as _BASE_INTERACTIVE_ROLES
from .cdp import CDPClient, CDPDocumentChangedError
from .cdp_persistent import CDPConnection as PersistentCDP
from .tiers import TierManager, ConnectionTier
from .registry import ElementRegistry
from .coordinator import AutomationCoordinator
from .observations import ElementRecord, ObservationSnapshot
from .operation import (
    OperationBudget,
    OperationError,
    OperationErrorCode,
    OperationMode,
)
from .provider_worker import ProviderCallState, ProviderWorker
from .result_format import BoundedResultFormatter, bounded_json_utf8_size
from .input_validation import InputValidationError, validate_tool_arguments
from .tool_contract import expose_shadow_target_ids, harden_tool_schemas
from . import platform_utils as _pu
from .__init__ import __version__
from .js_bridge import build_ax_tree_script, merge_pierced_nodes
# AppleScript is macOS-only — import conditionally
if sys.platform == "darwin":
    from . import applescript as _as
else:
    _as = None  # type: ignore

logger = logging.getLogger("agent-eyes")

_NativeResult = TypeVar("_NativeResult")


class _NativeMutationOutcomeUnknown(RuntimeError):
    """A sync mutation was submitted but its completion wasn't observed."""


@dataclass(frozen=True, slots=True)
class _AppleShadowResult:
    text: str
    invalidate_target: bool = False


async def _run_native_mutation(
    call: Callable[[], _NativeResult],
    *,
    budget: OperationBudget,
    operation: str,
    worker: ProviderWorker,
) -> _NativeResult:
    """Run one native mutation without collapsing pre/post-dispatch deadlines."""
    state = ProviderCallState()
    try:
        return await worker.run(
            call,
            budget=budget,
            operation=operation,
            state=state,
        )
    except (OperationError, asyncio.CancelledError) as exc:
        if state.may_have_run:
            coordinator.poison_foreground_until(worker.wait_until_idle())
            raise _NativeMutationOutcomeUnknown(operation) from exc
        raise


async def _with_timeout(coro, seconds: float, operation: str):
    """Wrap any async operation with a timeout and clear error."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        return f"✗ {operation}: timed out after {seconds}s"


# ── Platform detection ──────────────────────────────────────────────
def _get_native_adapter() -> BaseAdapter | None:
    """Return the current platform adapter without mutating the runtime."""
    from .setup.readiness import load_native_provider

    return load_native_provider()


def load_input_provider():
    """Load physical input lazily while preserving the server test seam."""
    from .setup.readiness import load_input_provider as _load_input_provider

    return _load_input_provider()


def probe_native_capability(native_provider, *, platform_name=None):
    """Probe native access lazily while preserving the server test seam."""
    from .setup.readiness import probe_native_capability as _probe_native_capability

    return _probe_native_capability(native_provider, platform_name=platform_name)


def probe_input_capability(input_provider):
    """Probe physical input lazily while preserving the server test seam."""
    from .setup.readiness import probe_input_capability as _probe_input_capability

    return _probe_input_capability(input_provider)


def compose_readiness_report(**kwargs):
    """Compose readiness lazily so importing the MCP server stays fast."""
    from .setup.readiness import compose_readiness_report as _compose_readiness_report

    return _compose_readiness_report(**kwargs)


async def run_native_action_until(*args, **kwargs):
    """Observe native action completion without loading providers at startup."""
    from .native_events import run_native_action_until as _run_native_action_until

    return await _run_native_action_until(*args, **kwargs)


async def wait_for_native_element(*args, **kwargs):
    """Wait for native elements without loading event backends at startup."""
    from .native_events import wait_for_native_element as _wait_for_native_element

    return await _wait_for_native_element(*args, **kwargs)


def collect_browser_targets(adapter, *, tree_depth: int = 8):
    """Inventory foreground browser targets without startup-time policy imports."""
    from .browser_inventory import collect_browser_targets as _collect_browser_targets

    return _collect_browser_targets(adapter, tree_depth=tree_depth)


def best_browser_target(targets, query: str, *, minimum_score: int = 60):
    """Select the strongest reusable target through the lazy inventory policy."""
    from .browser_inventory import best_browser_target as _best_browser_target

    return _best_browser_target(targets, query, minimum_score=minimum_score)


def select_browser_target(adapter, target):
    """Select one native target through the lazy inventory policy."""
    from .browser_inventory import select_browser_target as _select_browser_target

    return _select_browser_target(adapter, target)


def sanitize_url_for_display(value: str) -> str:
    """Redact a URL through the lazy inventory policy."""
    from .browser_inventory import sanitize_url_for_display as _sanitize_url_for_display

    return _sanitize_url_for_display(value)


def format_browser_targets(
    targets,
    *,
    query: str = "",
    max_query_results: int = 10,
) -> str:
    """Render browser inventory through the lazy inventory policy."""
    from .browser_inventory import format_browser_targets as _format_browser_targets

    return _format_browser_targets(
        targets,
        query=query,
        max_query_results=max_query_results,
    )


# ── Server setup ────────────────────────────────────────────────────
registry = ElementRegistry()
coordinator = AutomationCoordinator()
native_worker = ProviderWorker("native-accessibility")
input_worker = ProviderWorker("physical-input")
apple_worker = ProviderWorker("apple-events")
system_worker = ProviderWorker("system")
_result_formatter = BoundedResultFormatter()
native_adapter: BaseAdapter | None = None
_input_backend: object | None = None
_runtime_readiness = None
_runtime_readiness_lock = threading.Lock()
_runtime_async_locks_guard = threading.Lock()
_runtime_async_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()
_RUNTIME_READINESS_TIMEOUT_SECONDS = 5.0
_NATIVE_TREE_PROVIDER_TIMEOUT_SECONDS = 30.0


def _initialization_instructions() -> str:
    return (
        "Agent Eyes checks local capabilities on the first status or computer-use request. "
        "Reuse relevant open browser tabs before opening a new tab; use foreground native "
        "automation by default and shadow mode only when explicitly requested. Run "
        "`agent-eyes setup` if status reports a missing dependency or permission."
    )


app = Server(
    "agent-eyes",
    version=__version__,
    instructions=_initialization_instructions(),
)
cdp_client = CDPClient()
tier_manager = TierManager()
cdp_pool = PersistentCDP()


def _platform_status() -> str:
    if _runtime_readiness is None:
        return "Native and input providers: not checked yet (call status)"
    parts = []
    if native_adapter:
        ok, msg = native_adapter.check_permissions()
        parts.append(f"Native adapter: {native_adapter.__class__.__name__} — {msg}")
    else:
        parts.append("Native adapter: NOT AVAILABLE (missing dependencies)")
    parts.append("Shadow browser provider (optional): check with list_tabs")
    return "\n".join(parts)


# ── Tool definitions ────────────────────────────────────────────────
TOOLS = harden_tool_schemas(expose_shadow_target_ids([
    Tool(
        name="status",
        description=(
            "Run live local readiness checks for the native accessibility and input providers. "
            "This never probes a browser or starts installation."
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
            "Default: returns only interactive elements as flat one-liners (token-efficient). "
            "Each element has an [id] you can use with click/type. "
            "This is the PRIMARY way to 'see' an application — no screenshot needed. "
            "Browsers are inspected through the same foreground OS accessibility provider "
            "as every other application; no remote browser connection is opened. "
            "Set full=True for the complete nested tree. "
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
                "interactive_only": {
                    "type": "boolean",
                    "description": "Return only interactive elements as flat one-liners (default: true)",
                    "default": True,
                },
                "max_items": {
                    "type": "integer",
                    "description": "Maximum interactive rows returned (default 80, max 200)",
                    "default": 80,
                    "minimum": 1,
                    "maximum": 200,
                },
                "full": {
                    "type": "boolean",
                    "description": "Return full nested tree (overrides interactive_only, default: false)",
                    "default": False,
                },
                "timeout": {
                    "type": "number",
                    "description": "Total operation deadline in seconds (default 5)",
                    "default": 5.0,
                    "minimum": 0,
                    "maximum": 30,
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
                    "description": "Match type: 'contains' (default), 'exact', 'prefix', or 'suffix'",
                    "default": "contains",
                    "enum": ["contains", "exact", "prefix", "suffix"],
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
                "snapshot": {
                    "type": "string",
                    "description": "Snapshot token returned by tree/web_tree",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicitly use a shadow snapshot/provider (default: false)",
                    "default": False,
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
                "snapshot": {
                    "type": "string",
                    "description": "Snapshot token returned by tree/web_tree",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicitly use a shadow snapshot/provider (default: false)",
                    "default": False,
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
            "Scan already-open tabs and windows across supported browsers using the "
            "foreground OS accessibility provider. Pass query to rank likely reusable "
            "tabs while scanning every target visible to the native provider. This never starts or probes "
            "remote browser mode by default. Set shadow=true only when background CDP "
            "tab metadata was explicitly requested."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional task, title, domain, or URL terms used to rank reusable tabs",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicitly probe the optional background/CDP provider (default: false)",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum ranked matches returned when query is set (default: 10, max: 50)",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="web_tree",
        description=(
            "Get a DOM-backed accessibility tree through the optional shadow provider. "
            "This is background/protocol automation and requires explicit shadow=true; "
            "for normal visible browser work use tree with the browser PID. "
            "Default: returns only interactive elements as flat one-liners (token-efficient). "
            "Set full=True for the complete nested tree."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "max_depth": {
                    "type": "integer",
                    "description": "Max tree depth (default 5)",
                    "default": 5,
                },
                "interactive_only": {
                    "type": "boolean",
                    "description": "Return only interactive elements as flat one-liners (default: true)",
                    "default": True,
                },
                "full": {
                    "type": "boolean",
                    "description": "Return full nested tree (overrides interactive_only, default: false)",
                    "default": False,
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicit consent to use the optional background browser provider",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    # ── New tools ──────────────────────────────────────────────────
    Tool(
        name="navigate",
        description=(
            "Reuse a matching visible browser tab or open the URL in the user's default browser. "
            "Foreground native behavior is the default. Set shadow=true only when navigating an "
            "explicit background provider tab."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to",
                },
                "query": {
                    "type": "string",
                    "description": "Optional intent terms used to reuse a relevant foreground tab",
                },
                "reuse_existing": {
                    "type": "boolean",
                    "description": "Check existing foreground tabs before opening (default: true)",
                    "default": True,
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional background provider instead of foreground automation",
                    "default": False,
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="js",
        description=(
            "Execute JavaScript through the optional background browser provider. "
            "Protocol-only capability; requires explicit shadow=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicit consent to use the optional background browser provider",
                    "default": False,
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
            "With no PID it targets the current foreground app. "
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
                    "description": "Target foreground app or browser PID",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional background provider instead of OS input",
                    "default": False,
                },
            },
            "required": ["key"],
        },
    ),
    Tool(
        name="wait",
        description=(
            "Wait for an element to appear using accessibility or browser lifecycle events. "
            "Foreground waiting requires pid; shadow waiting must be explicit."
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
                "pid": {
                    "type": "integer",
                    "description": "Process ID for foreground native waiting",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional event-driven CDP provider (default: false)",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="new_tab",
        description=(
            "Reuse a suitable already-open foreground browser tab when possible; "
            "otherwise open the URL in the user's default browser. Pass query to "
            "describe the desired tab. Existing-tab reuse is on by default. Set "
            "shadow=true only for an explicitly requested background CDP tab."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open (default: about:blank)",
                    "default": "about:blank",
                },
                "query": {
                    "type": "string",
                    "description": "Task, title, domain, or URL terms used to find a reusable open tab",
                },
                "reuse_existing": {
                    "type": "boolean",
                    "description": "Reuse a suitable foreground tab before opening a new one (default: true)",
                    "default": True,
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Open through the optional background CDP provider (default: false)",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="close_tab",
        description=(
            "Close a visible tab in any supported browser through native foreground control. "
            "Always call list_tabs immediately first and prefer title for safe targeting. "
            "Set shadow=true only for an explicit background-provider tab."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Substring to match against tab titles (case-insensitive). "
                        "Must match exactly one fresh target."
                    ),
                },
                "target_id": {
                    "type": "string",
                    "description": "Stable native target ID from the latest list_tabs result",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Close through the optional background provider",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="dialog",
        description=(
            "Handle a protocol-level JavaScript dialog through the optional shadow provider. "
            "Requires explicit shadow=true; visible native dialogs use tree and click."
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
                "shadow": {
                    "type": "boolean",
                    "description": "Explicit consent to use the optional background provider",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="upload",
        description=(
            "Inject file(s) into a DOM file input through the optional shadow provider. "
            "Requires explicit shadow=true and absolute file paths."
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
                "shadow": {
                    "type": "boolean",
                    "description": "Explicit consent to use the optional background provider",
                    "default": False,
                },
            },
            "required": ["id", "files"],
        },
    ),
    Tool(
        name="scroll",
        description=(
            "Scroll the current foreground app or a provided PID using OS input. "
            "Use positive delta_y to scroll down; set shadow=true only for background scrolling."
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
                "pid": {
                    "type": "integer",
                    "description": "Optional foreground application or browser PID",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional background provider instead of OS input",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="drag",
        description=(
            "Drag between screen coordinates using foreground OS input. "
            "Set shadow=true only for background browser drag."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "from_x": {"type": "integer", "description": "Start X coordinate"},
                "from_y": {"type": "integer", "description": "Start Y coordinate"},
                "to_x": {"type": "integer", "description": "End X coordinate"},
                "to_y": {"type": "integer", "description": "End Y coordinate"},
                "pid": {
                    "type": "integer",
                    "description": "Optional foreground application or browser PID",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional background provider instead of OS input",
                    "default": False,
                },
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
    ),
    Tool(
        name="fill_form",
        description=(
            "Fill multiple foreground form fields by [id] from a native accessibility tree. "
            "Set shadow=true only for fields returned by an explicit shadow web_tree."
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
                "shadow": {
                    "type": "boolean",
                    "description": "Use the optional background provider for shadow-tree fields",
                    "default": False,
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
                "snapshot": {
                    "type": "string",
                    "description": "Snapshot token returned by window(action='list')",
                },
                "id": {
                    "type": "integer",
                    "description": "Exact native window ID returned by window(action='list')",
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
            "without traversing a full tree. One compact call for initial orientation; "
            "use tree or subtree only when more detail is needed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fast": {
                    "type": "boolean",
                    "description": (
                        "If true, return only app name, window title, and focused element "
                        "(compact mode is the default; retained for compatibility)."
                    ),
                    "default": True,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="shadow",
        description=(
            "Execute browser actions through the optional background Chromium provider. "
            "Actions: 'click' (by text/selector), 'type' (into focused/selected element), "
            "'press_key' (Enter/Tab/Escape/etc), 'scroll' (up/down), "
            "'read' (get all interactive elements), 'js' (raw JavaScript). "
            "This tool is explicit shadow mode and does not focus the visible browser."
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
            },
            "required": ["action"],
        },
    ),
    Tool(
        name="pierce",
        description=(
            "Inspect shadow DOM elements inside a CSS selector's shadow root. "
            "Modern web apps (Salesforce, Shopify, GitHub, Google) use shadow DOM to "
            "encapsulate components — standard accessibility trees miss these elements. "
            "Use pierce to see what's inside a shadow root: roles, names, and IDs for interaction. "
            "Returns a flat list from the optional protocol provider and requires explicit shadow=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector identifying the element whose shadow root to inspect (e.g. 'custom-nav', '#app-shell')",
                },
                "shadow": {
                    "type": "boolean",
                    "description": "Explicit consent to use the optional background provider",
                    "default": False,
                },
            },
            "required": ["selector"],
        },
    ),
    Tool(
        name="install_check",
        description=(
            "Compatibility alias for live Agent Eyes readiness. "
            "Use status for new integrations; use the agent-eyes setup CLI to repair missing capabilities."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
]))

if sys.platform != "darwin":
    TOOLS[:] = [tool for tool in TOOLS if tool.name not in {"app", "window"}]


# Keep tools/list small: property names, enums, defaults, and enforced bounds carry
# the leaf-level contract. Consent and routing guidance stays once at tool level.
_COMPACT_TOOL_DESCRIPTIONS = {
    "status": "Check native accessibility/input readiness; never opens a browser or installs.",
    "list_apps": "List visible apps/windows; use a PID with tree.",
    "tree": (
        "Inspect a PID through foreground native accessibility and return actionable [id]s. "
        "For browsers prefer this; use web_tree with explicit shadow=true only if requested."
    ),
    "find": "Find loaded native-tree elements by role/name/value; optional pid refreshes the tree.",
    "click": (
        "Click a tree/web_tree [id] (qualify with snapshot), or native x/y with pid. "
        "Set shadow=true only for an explicit background snapshot."
    ),
    "type": (
        "Type text into a tree/web_tree [id] qualified by snapshot. Native input is the "
        "foreground default; shadow=true is an explicit background opt-in."
    ),
    "focused": "Return the focused UI element.",
    "list_tabs": (
        "Scan and rank all open browser tabs via foreground native accessibility; reuse one "
        "before opening. shadow=true explicitly opts into background protocol metadata."
    ),
    "web_tree": (
        "Get a background protocol DOM tree; requires explicit shadow=true and target_id from "
        "list_tabs. Prefer tree(pid) for foreground browser work."
    ),
    "navigate": (
        "Reuse a matching foreground tab before opening URL in the default browser. "
        "shadow=true explicitly opts into a background target_id from list_tabs."
    ),
    "js": (
        "Run JavaScript in a background protocol target_id from list_tabs; requires explicit "
        "shadow=true."
    ),
    "press_key": (
        "Press key/modifiers in the foreground app (optional pid). shadow=true explicitly uses "
        "a background target_id from list_tabs."
    ),
    "wait": (
        "Wait event-first for a foreground native element (pid). shadow=true explicitly waits "
        "on a background target_id from list_tabs."
    ),
    "new_tab": (
        "Reuse a matching foreground open tab, else open URL in the default browser. "
        "shadow=true explicitly opens a background protocol tab."
    ),
    "close_tab": (
        "Close a foreground tab; first list_tabs and prefer title/target_id. shadow=true "
        "explicitly closes a background target_id."
    ),
    "dialog": (
        "Handle a background JavaScript dialog by target_id; requires explicit shadow=true. "
        "Use tree/click for a visible native dialog."
    ),
    "upload": (
        "Set a DOM file input; requires explicit shadow=true, a web_tree snapshot, and absolute "
        "file paths."
    ),
    "scroll": (
        "Scroll the foreground app (optional pid) via OS input. shadow=true explicitly scrolls "
        "a background target_id."
    ),
    "drag": (
        "Drag between screen points via foreground OS input. shadow=true explicitly drags in a "
        "background target_id."
    ),
    "fill_form": (
        "Fill tree [id]/value pairs in the foreground. shadow=true explicitly uses web_tree "
        "snapshot fields."
    ),
    "hover": "Hover a tree [id] or screen x/y via foreground OS input.",
    "app": "Launch, quit, or focus an app by name or bundle ID.",
    "subtree": "Expand a prior tree [id] without fetching the whole app tree.",
    "window": "List, focus, minimize, close, move, or resize native app windows.",
    "context": "Return compact frontmost app, window, and focus context without a full tree.",
    "shadow": (
        "Explicit background Chromium legacy click/type/key/scroll/read/JS; never focuses the "
        "visible browser."
    ),
    "pierce": (
        "Inspect shadow-DOM elements in a background target_id from list_tabs; requires explicit "
        "shadow=true."
    ),
    "install_check": "Compatibility readiness alias; prefer status and agent-eyes setup to repair.",
}

if sys.platform != "darwin":
    _COMPACT_TOOL_DESCRIPTIONS.pop("app", None)
    _COMPACT_TOOL_DESCRIPTIONS.pop("window", None)


def _drop_schema_descriptions(value: object) -> None:
    if isinstance(value, dict):
        value.pop("description", None)
        for child in value.values():
            _drop_schema_descriptions(child)
    elif isinstance(value, list):
        for child in value:
            _drop_schema_descriptions(child)


if set(_COMPACT_TOOL_DESCRIPTIONS) != {tool.name for tool in TOOLS}:
    raise RuntimeError("compact tool descriptions must cover the complete catalog")
for _tool in TOOLS:
    _tool.description = _COMPACT_TOOL_DESCRIPTIONS[_tool.name]
    _drop_schema_descriptions(_tool.inputSchema)


_READINESS_STATUS_TOOLS = frozenset({"status", "install_check"})
_TOOL_SCHEMAS = {tool.name: tool.inputSchema for tool in TOOLS}
_NATIVE_CAPABILITY = "native_access"
_INPUT_CAPABILITY = "input"
_NATIVE_ONLY_TOOLS = frozenset(
    {
        "list_apps",
        "tree",
        "focused",
        "subtree",
        "context",
        "window",
        "type",
        "fill_form",
        "wait",
    }
)
_INPUT_ONLY_TOOLS = frozenset({"press_key", "scroll", "drag"})


def _required_runtime_capabilities(name: str, arguments: dict) -> frozenset[str]:
    """Return only the local execution-plane capabilities needed by this call."""
    if name in _READINESS_STATUS_TOOLS or arguments.get("shadow", False):
        return frozenset()
    if name in _NATIVE_ONLY_TOOLS:
        return frozenset({_NATIVE_CAPABILITY})
    if name in _INPUT_ONLY_TOOLS:
        return frozenset({_INPUT_CAPABILITY})
    if name == "find":
        return (
            frozenset({_NATIVE_CAPABILITY})
            if arguments.get("pid") is not None
            else frozenset()
        )
    if name == "click":
        if arguments.get("x") is not None and arguments.get("y") is not None:
            return frozenset({_INPUT_CAPABILITY})
        return frozenset({_NATIVE_CAPABILITY})
    if name == "hover":
        if arguments.get("x") is not None and arguments.get("y") is not None:
            return frozenset({_INPUT_CAPABILITY})
        return frozenset({_NATIVE_CAPABILITY, _INPUT_CAPABILITY})
    if name == "list_tabs":
        return frozenset({_NATIVE_CAPABILITY})
    if name in {"navigate", "new_tab"}:
        url = str(arguments.get("url", "about:blank"))
        query = str(arguments.get("query", "")).strip()
        should_reuse = bool(arguments.get("reuse_existing", True))
        if should_reuse and (query or url != "about:blank"):
            return frozenset({_NATIVE_CAPABILITY})
        return frozenset()
    if name == "close_tab":
        return frozenset({_NATIVE_CAPABILITY, _INPUT_CAPABILITY})
    return frozenset()


def _unavailable_runtime_capabilities(report, required: frozenset[str]) -> list:
    """Resolve unavailable required probes, tolerating narrow test doubles."""
    if not required:
        return []
    capability = getattr(report, "capability", None)
    if callable(capability):
        return [probe for name in sorted(required) if not (probe := capability(name)).available]
    return [] if bool(getattr(report, "core_ready", False)) else sorted(required)


def _runtime_capability_error(unavailable: list) -> str:
    permission_required = any(
        getattr(probe, "status", "") == "permission_required"
        for probe in unavailable
    )
    status = "permission_required" if permission_required else "setup_required"
    names = ", ".join(
        getattr(probe, "name", str(probe))
        for probe in unavailable
    )
    return (
        f"{status}: Required runtime capability unavailable: {names}.\n"
        "Recovery: agent-eyes setup"
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent] | CallToolResult:
    try:
        schema = _TOOL_SCHEMAS.get(name)
        if schema is None:
            bounded = _result_formatter.format(f"ERROR: Unknown tool: {name}")
            return CallToolResult(
                content=[TextContent(type="text", text=bounded.text)],
                isError=True,
            )
        validate_tool_arguments(schema, arguments)
        required_capabilities = _required_runtime_capabilities(name, arguments)
        if required_capabilities:
            runtime_readiness = await _ensure_runtime_readiness()
            unavailable = _unavailable_runtime_capabilities(
                runtime_readiness,
                required_capabilities,
            )
            if unavailable:
                bounded = _result_formatter.format(_runtime_capability_error(unavailable))
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=bounded.text,
                        )
                    ],
                    isError=True,
                )
        result = await _dispatch(name, arguments)
        bounded = _result_formatter.format(result)
        if result.startswith("ERROR:"):
            return CallToolResult(
                content=[TextContent(type="text", text=bounded.text)],
                isError=True,
            )
        return [TextContent(type="text", text=bounded.text)]
    except OperationError as exc:
        bounded = _result_formatter.format(f"ERROR: {exc}")
        return CallToolResult(
            content=[TextContent(type="text", text=bounded.text)],
            isError=True,
        )
    except InputValidationError as exc:
        bounded = _result_formatter.format(f"ERROR: Invalid input: {exc}")
        return CallToolResult(
            content=[TextContent(type="text", text=bounded.text)],
            isError=True,
        )
    except Exception as exc:
        # Exception messages can contain typed text, JavaScript, file paths, or
        # provider-returned values.  Keep logs diagnostic without echoing them.
        traceback = exc.__traceback__
        location = "unknown"
        while traceback is not None:
            location = f"{traceback.tb_frame.f_code.co_name}:{traceback.tb_lineno}"
            traceback = traceback.tb_next
        logger.error(
            "Tool '%s' failed with %s at %s",
            name,
            type(exc).__name__,
            location,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"ERROR: Tool '{name}' failed unexpectedly. Check server logs for details.",
                )
            ],
            isError=True,
        )


# Dispatch table — built lazily on first call so all handlers are defined.
_DISPATCH_TABLE: dict[str, object] | None = None


def _build_dispatch_table() -> dict[str, object]:
    """O(1) lookup instead of 30+ if/elif branches."""
    return {
        "status": lambda args: _handle_status(),
        "list_apps": lambda args: _handle_list_apps_async(),
        "tree": _handle_get_tree,
        "find": _handle_find_async,
        "click": _handle_click,
        "type": _handle_type,
        "focused": lambda args: _handle_get_focused_async(),
        "list_tabs": _handle_list_tabs,
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
        "app": _handle_app_async,
        "subtree": _handle_get_subtree_async,
        "window": _handle_window,
        "context": _handle_context_async,
        "shadow": _handle_shadow_async,
        "pierce": _handle_pierce,
        "install_check": lambda args: _handle_install_check(),
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
async def _ensure_runtime_readiness(*, refresh: bool = False):
    """Await real provider readiness without blocking the MCP event loop."""
    global native_adapter, _input_backend, _runtime_readiness

    if _runtime_readiness is not None and not refresh:
        return _runtime_readiness

    loop = asyncio.get_running_loop()
    with _runtime_async_locks_guard:
        async_lock = _runtime_async_locks.get(loop)
        if async_lock is None:
            async_lock = asyncio.Lock()
            _runtime_async_locks[loop] = async_lock

    async with async_lock:
        if _runtime_readiness is not None and not refresh:
            return _runtime_readiness
        budget = OperationBudget.start(_RUNTIME_READINESS_TIMEOUT_SECONDS)

        # Construct provider-owned clients on the same bounded lane that will
        # use them. In particular, Windows UIA/COM stays on the native lane and
        # Linux Xlib Display stays on the physical-input lane.
        provider_loads = []
        if native_adapter is None:
            provider_loads.append(
                ("native", native_worker.run(
                    _get_native_adapter,
                    budget=budget,
                    operation="native provider construction",
                ))
            )
        if _input_backend is None:
            provider_loads.append(
                ("input", input_worker.run(
                    load_input_provider,
                    budget=budget,
                    operation="input provider construction",
                ))
            )
        if provider_loads:
            loaded = await asyncio.gather(*(item[1] for item in provider_loads))
            with _runtime_readiness_lock:
                for (lane, _), provider in zip(provider_loads, loaded):
                    if lane == "native" and native_adapter is None:
                        native_adapter = provider
                    elif lane == "input" and _input_backend is None:
                        _input_backend = provider

        native_probe, input_probe = await asyncio.gather(
            native_worker.run(
                lambda: probe_native_capability(native_adapter),
                budget=budget,
                operation="native readiness probe",
            ),
            input_worker.run(
                lambda: probe_input_capability(_input_backend),
                budget=budget,
                operation="input readiness probe",
            ),
        )
        report = compose_readiness_report(
            native_probe=native_probe,
            input_probe=input_probe,
        )
        with _runtime_readiness_lock:
            _runtime_readiness = report
        return report


async def _refresh_runtime_readiness():
    """Refresh permissions without installation, network, or browser probes."""
    return await _ensure_runtime_readiness(refresh=True)


async def _handle_status() -> str:
    return (await _refresh_runtime_readiness()).to_text()


async def _handle_install_check() -> str:
    """Compatibility alias backed by the same live readiness report as status."""
    return await _handle_status()


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


async def _handle_list_apps_async() -> str:
    return await native_worker.run(
        _handle_list_apps,
        budget=OperationBudget.start(5.0),
        operation="native application inventory",
    )


def _snapshot_native_tree(
    tree: UIElement,
    pid: int,
) -> tuple[str, int, frozenset[int]]:
    """Create one immutable native observation without relying on registry state."""
    records: list[ElementRecord] = []
    pending = [tree]
    while pending and len(records) < 500:
        element = pending.pop()
        element.pid = pid
        records.append(ElementRecord(local_id=element.id, value=element))
        pending.extend(reversed(element.children))
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id=f"pid:{pid}",
        generation=0,
        revision=time.monotonic_ns(),
        elements=records,
    )
    return snapshot.token, len(records), frozenset(record.local_id for record in records)


def _resolve_action_element(
    args: dict,
    action: str,
) -> tuple[
    UIElement | None,
    ObservationSnapshot | None,
    OperationMode,
    str,
    str,
]:
    element_id = args.get("id")
    mode = OperationMode.SHADOW if args.get("shadow", False) else OperationMode.FOREGROUND
    if element_id is None:
        return None, None, mode, "", f"ERROR: {action}: id is required."

    token = args.get("snapshot", "")
    try:
        if token:
            snapshot, record = coordinator.observations.resolve_with_snapshot(
                token,
                element_id,
                expected_mode=mode,
            )
        else:
            snapshot, record = coordinator.observations.resolve_legacy(
                element_id,
                expected_mode=mode,
            )
        action_key = snapshot.target_id
    except OperationError as exc:
        return None, None, mode, "", f"ERROR: {action} [{element_id}]: {exc}"

    if not record.actionable:
        return (
            None,
            snapshot,
            mode,
            "",
            f"ERROR: {action} [{element_id}]: UNSUPPORTED_CAPABILITY: element is read-only",
        )

    element = record.value
    if not isinstance(element, UIElement):
        return (
            None,
            snapshot,
            mode,
            "",
            f"ERROR: {action} [{element_id}]: snapshot record is not a UI element",
        )
    if mode is OperationMode.FOREGROUND and element.source in {"cdp", "shadow-dom"}:
        return (
            None,
            snapshot,
            mode,
            "",
            f"ERROR: {action} [{element_id}]: MODE_MISMATCH: shadow=true is required",
        )
    if mode is OperationMode.SHADOW and element.source not in {"cdp", "shadow-dom"}:
        return (
            None,
            snapshot,
            mode,
            "",
            f"ERROR: {action} [{element_id}]: MODE_MISMATCH: element is foreground-native",
        )
    return element, snapshot, mode, action_key, ""


async def _handle_get_tree(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."
    pid = args.get("pid")
    if pid is None:
        return "ERROR: pid is required."

    full = args.get("full", False)
    interactive_only = args.get("interactive_only", True) and not full
    max_items = max(1, min(int(args.get("max_items", 80)), 200))
    budget = OperationBudget.start(float(args.get("timeout", 5.0)))

    # Universal depth: default 10, max 20.
    max_depth = min(args.get("max_depth", 10), 20)

    async def load_tree(provider_budget: OperationBudget):
        is_browser = await system_worker.run(
            lambda: _pu.is_browser_pid(pid),
            budget=provider_budget,
            operation="browser process classification",
        )
        loaded = await native_worker.run(
            lambda: native_adapter.get_tree(pid, max_depth, is_browser=is_browser),
            budget=provider_budget,
            operation="native accessibility tree",
        )
        if loaded is None:
            return None, False, 0, max_depth

        has_web_content, interactive_count = _analyze_tree(loaded)
        used_depth = max_depth
        if has_web_content and interactive_count < 5 and max_depth < 20:
            loaded = await native_worker.run(
                lambda: native_adapter.get_tree(pid, 20, is_browser=is_browser),
                budget=provider_budget,
                operation="deep native accessibility tree",
            )
            if loaded is None:
                return None, has_web_content, 0, 20
            has_web_content, interactive_count = _analyze_tree(loaded)
            used_depth = 20
        return loaded, has_web_content, interactive_count, used_depth

    async def serialized_load():
        provider_budget = OperationBudget.start(_NATIVE_TREE_PROVIDER_TIMEOUT_SECONDS)
        return await coordinator.execute_foreground(
            lambda: load_tree(provider_budget),
            budget=provider_budget,
        )

    tree, has_web, interactive_count, max_depth = await coordinator.observe(
        ("native-tree", id(native_adapter), pid, max_depth),
        serialized_load,
        budget=budget,
    )

    if tree is None:
        return f"ERROR: Could not get accessibility tree for PID {pid}. App may not be running or permission denied."

    registry.register_tree(tree, pid=pid)
    snapshot_token, snapshot_count, snapshot_ids = _snapshot_native_tree(tree, pid)

    # interactive_only mode: flat one-liner list of interactive elements
    if interactive_only:
        elements: list[UIElement] = []
        _collect_interactive_flat(
            tree,
            elements,
            _BASE_INTERACTIVE_ROLES,
            max_items=max_items,
            allowed_ids=snapshot_ids,
        )
        if not elements:
            return (
                f"snapshot={snapshot_token}\n"
                f"No interactive elements found in PID {pid}. Use full=True for the complete tree."
            )
        lines = [el.to_flat_line() for el in elements]
        if len(elements) >= max_items:
            lines.append(
                f"… output limit {max_items} reached; narrow with find/subtree or raise max_items."
            )
        return f"snapshot={snapshot_token}\n" + "\n".join(lines)

    # full mode: existing nested format
    text = _render_snapshot_tree(tree, snapshot_ids, max_depth=max_depth)

    # Metadata and advisories
    meta = f"Accessibility tree for PID {pid} ({snapshot_count} elements"
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
        f"snapshot={snapshot_token}\n{meta}\n\n"
        f"{text}{advisory}\n\n"
        f"Use [id] numbers with click or type to interact."
    )


def _collect_interactive_flat(
    element: UIElement,
    results: list,
    roles: frozenset,
    max_items: int = 200,
    allowed_ids: frozenset[int] | None = None,
) -> None:
    """Walk the tree and collect elements whose role is in roles."""
    if len(results) >= max_items:
        return
    if allowed_ids is not None and element.id not in allowed_ids:
        return
    if element.role in roles:
        results.append(element)
    for child in element.children:
        _collect_interactive_flat(
            child,
            results,
            roles,
            max_items,
            allowed_ids,
        )


def _render_snapshot_tree(
    element: UIElement,
    allowed_ids: frozenset[int],
    *,
    max_depth: int,
    depth: int = 0,
) -> str:
    """Render only element IDs owned by one immutable snapshot."""
    if depth > max_depth or element.id not in allowed_ids:
        return ""
    lines = [element.to_text(depth=depth, max_depth=depth)]
    for child in element.children:
        rendered = _render_snapshot_tree(
            child,
            allowed_ids,
            max_depth=max_depth,
            depth=depth + 1,
        )
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


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
    else:  # contains (default)
        return query_lower in text_lower


def _handle_find(args: dict) -> str:
    pid = args.get("pid")
    role = args.get("role", "")
    name = args.get("name", "")
    value = args.get("value", "")
    match_type = args.get("match", "contains")

    if match_type not in {"contains", "exact", "prefix", "suffix"}:
        return "ERROR: match must be one of: contains, exact, prefix, suffix"

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


async def _handle_find_async(args: dict) -> str:
    role = str(args.get("role", ""))
    name = str(args.get("name", ""))
    value = str(args.get("value", ""))
    match_type = str(args.get("match", "contains"))
    if match_type not in {"contains", "exact", "prefix", "suffix"}:
        return "ERROR: match must be one of: contains, exact, prefix, suffix"
    if not role and not name and not value:
        return "ERROR: Specify at least one of: role, name, value"

    pid = args.get("pid")
    snapshot_token = str(args.get("snapshot", "")).strip()
    source_snapshot: ObservationSnapshot | None = None
    if pid is not None:
        if native_adapter is None:
            return "ERROR: No native adapter available."
        elements = await native_worker.run(
            lambda: native_adapter.find_elements(int(pid), role, name, value),
            budget=OperationBudget.start(5.0),
            operation="native element search",
        )
        provider = "native"
        target_id = f"pid:{int(pid)}"
        generation = 0
    elif snapshot_token:
        try:
            source_snapshot = coordinator.observations.get_snapshot(
                snapshot_token,
                expected_mode=OperationMode.FOREGROUND,
            )
        except OperationError as exc:
            return f"ERROR: find: {exc}"
        elements = [
            record.value
            for record in source_snapshot.elements
            if isinstance(record.value, UIElement)
        ]
        provider = source_snapshot.provider
        target_id = source_snapshot.target_id
        generation = source_snapshot.generation
    else:
        return "ERROR: find requires pid or snapshot from tree."

    elements = [
        element
        for element in elements
        if (not role or _match_text(role, element.role, match_type))
        and (not name or _match_text(name, element.name, match_type))
        and (not value or _match_text(value, element.value, match_type))
    ][:500]
    if not elements:
        return "No matching elements found."
    registry.register_elements(elements)
    source_actions = (
        {record.local_id: record.actionable for record in source_snapshot.elements}
        if source_snapshot is not None
        else {}
    )
    snapshot = coordinator.observations.create(
        provider=provider,
        mode=OperationMode.FOREGROUND,
        target_id=target_id,
        generation=generation,
        revision=time.monotonic_ns(),
        elements=[
            ElementRecord(
                local_id=element.id,
                value=element,
                actionable=source_actions.get(element.id, True),
            )
            for element in elements
        ],
    )
    lines = [
        f"snapshot={snapshot.token}",
        f"Found {len(elements)} matching element(s):",
    ]
    lines.extend(element.to_text(max_depth=1) for element in elements[:20])
    if len(elements) > 20:
        lines.append(f"... and {len(elements) - 20} more")
    return "\n".join(lines)


async def _verify_focus(
    pid: int,
    timeout: float = 0.5,
    retries: int = 3,
    *,
    budget: OperationBudget | None = None,
) -> tuple[bool, str]:
    """Activate an app and observe when it becomes frontmost.

    The accessibility subscription is active before activation, preventing a
    fast focus notification from being lost. ``retries`` is retained for API
    compatibility; activation itself is intentionally attempted only once.
    """
    input_backend = _input_backend
    operation_budget = budget or OperationBudget.start(timeout)
    if input_backend is None:
        return False, "Foreground input provider is unavailable"
    available = await input_worker.run(
        input_backend.is_available,
        budget=operation_budget,
        operation="foreground input capability check",
    )
    if not available:
        return False, "Foreground input provider is unavailable"
    if await input_worker.run(
        lambda: input_backend.is_frontmost(pid),
        budget=operation_budget,
        operation="frontmost window check",
    ):
        return True, ""

    action_state = ProviderCallState()
    try:
        result = await run_native_action_until(
            pid,
            lambda: input_backend.activate_window(pid),
            lambda: input_backend.is_frontmost(pid),
            timeout=timeout,
            budget=operation_budget,
            action_worker=input_worker,
            condition_worker=input_worker,
            action_state=action_state,
        )
    except (OperationError, asyncio.CancelledError):
        if not action_state.may_have_run:
            raise
        coordinator.poison_foreground_until(input_worker.wait_until_idle())
        _invalidate_native_mutation_state(pid=pid)
        return (
            False,
            "OUTCOME_UNKNOWN: foreground activation may have completed; "
            "refresh the target before retrying",
        )
    if result.condition_met:
        return True, ""

    return False, f"Could not bring app (PID {pid}) to front within {timeout:.2f}s"


def _browser_target_is_selected(target) -> bool:
    """Read live tab selection using only the target's native provider ref."""
    try:
        return bool(native_adapter.is_element_selected(target.element))
    except Exception:
        return False


def _browser_target_window_is_focused(target) -> bool:
    """Verify the exact owning native window, never just the browser PID."""
    if target.window_element is None:
        return False
    try:
        return bool(native_adapter.is_window_focused(target.window_element))
    except Exception:
        return False


def _browser_target_is_exactly_active(target) -> bool:
    """Verify both owning window and tab selection from live native refs."""
    return bool(
        target.element is not None
        and _browser_target_window_is_focused(target)
        and _browser_target_is_selected(target)
    )


async def _activate_browser_target_and_wait(
    target,
    timeout: float = 0.75,
    *,
    budget: OperationBudget | None = None,
) -> bool:
    """Focus the exact native window, then select its provider-owned tab."""
    operation_budget = budget or OperationBudget.start(timeout)
    if target.window_element is None:
        return False
    focus_ok, focus_error = await _verify_focus(
        target.pid,
        timeout=timeout,
        budget=operation_budget,
    )
    if not focus_ok:
        if "OUTCOME_UNKNOWN" in focus_error:
            raise _NativeMutationOutcomeUnknown("browser window activation")
        return False

    if not await native_worker.run(
        lambda: _browser_target_window_is_focused(target),
        budget=operation_budget,
        operation="exact browser window verification",
    ):
        window_state = ProviderCallState()
        try:
            window_result = await run_native_action_until(
                target.pid,
                lambda: native_adapter.focus_window(target.window_element),
                lambda: _browser_target_window_is_focused(target),
                timeout=timeout,
                budget=operation_budget,
                action_worker=native_worker,
                condition_worker=native_worker,
                action_state=window_state,
            )
        except (OperationError, asyncio.CancelledError) as exc:
            if not window_state.may_have_run:
                raise
            coordinator.poison_foreground_until(native_worker.wait_until_idle())
            _invalidate_native_mutation_state(pid=target.pid)
            raise _NativeMutationOutcomeUnknown("browser window activation") from exc
        if not window_result.condition_met:
            return False

    if target.source == "native-window":
        return True
    if target.element is None:
        return False

    action_state = ProviderCallState()
    try:
        result = await run_native_action_until(
            target.pid,
            lambda: select_browser_target(native_adapter, target),
            lambda: _browser_target_is_selected(target),
            timeout=timeout,
            budget=operation_budget,
            action_worker=native_worker,
            condition_worker=native_worker,
            action_state=action_state,
        )
    except (OperationError, asyncio.CancelledError) as exc:
        if not action_state.may_have_run:
            raise
        coordinator.poison_foreground_until(native_worker.wait_until_idle())
        _invalidate_native_mutation_state(pid=target.pid)
        raise _NativeMutationOutcomeUnknown("browser target activation") from exc
    if not result.condition_met:
        return False
    return await native_worker.run(
        lambda: _browser_target_is_exactly_active(target),
        budget=operation_budget,
        operation="exact browser target verification",
    )


def _persistent_runtime_exception(result: object) -> dict | None:
    """Return a CDP Runtime exception envelope without exposing page-owned text."""
    if not isinstance(result, dict):
        return None
    details = result.get("exceptionDetails")
    return details if isinstance(details, dict) else None


def _persistent_runtime_exception_is_stale(result: object) -> bool:
    """Recognize only Agent Eyes' own pre-mutation stale-element marker."""
    details = _persistent_runtime_exception(result)
    if details is None:
        return False
    exception = details.get("exception")
    candidates = [details.get("text")]
    if isinstance(exception, dict):
        candidates.extend((exception.get("description"), exception.get("value")))
    return any(
        isinstance(candidate, str) and "STALE_ELEMENT" in candidate
        for candidate in candidates
    )


async def _handle_click(args: dict) -> str:
    budget = OperationBudget.start(5.0)
    if args.get("x") is not None and args.get("y") is not None:
        return await coordinator.execute_foreground(
            lambda: _handle_click_resolved(args, None, budget=budget),
            budget=budget,
            operation_manages_deadline=True,
        )

    element, snapshot, mode, action_key, error = _resolve_action_element(args, "click")
    if error:
        return error
    async def operation():
        return await _handle_click_resolved(
            args,
            element,
            snapshot=snapshot,
            budget=budget,
        )
    if mode is OperationMode.SHADOW:
        return await coordinator.execute_shadow(
            action_key,
            operation,
            budget=budget,
            operation_manages_deadline=True,
        )
    return await coordinator.execute_foreground(
        operation,
        budget=budget,
        operation_manages_deadline=True,
    )


async def _handle_click_resolved(
    args: dict,
    element: UIElement | None,
    *,
    snapshot: ObservationSnapshot | None = None,
    budget: OperationBudget | None = None,
) -> str:
    operation_budget = budget or OperationBudget.start(5.0)
    element_id = args.get("id")
    click_x = args.get("x")
    click_y = args.get("y")
    click_pid = args.get("pid")

    # Coordinate-based click (from OCR hints or manual)
    if click_x is not None and click_y is not None:
        input_backend = _input_backend
        if input_backend is None or not await input_worker.run(
            input_backend.is_available,
            budget=operation_budget,
            operation="coordinate click capability check",
        ):
            return "ERROR: No input backend available for coordinate click."
        if click_pid:
            focus_ok, focus_err = await _verify_focus(
                click_pid,
                budget=operation_budget,
            )
            if not focus_ok:
                return f"ERROR: {focus_err}. Click aborted to prevent wrong target."
        try:
            clicked = await _run_native_mutation(
                lambda: input_backend.click(click_x, click_y),
                budget=operation_budget,
                operation="coordinate click",
                worker=input_worker,
            )
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=click_pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: the coordinate click may have been applied; "
                "refresh the target before retrying."
            )
        if clicked:
            return f"clicked at ({click_x}, {click_y})"
        return f"ERROR: Could not click at ({click_x}, {click_y})."

    if element_id is None:
        return "ERROR: id is required (or provide x, y coordinates)."

    if element is None:
        return f"ERROR: click [{element_id}]: element not found in observation"

    # Validate element reference is still alive (app may have navigated, window closed)
    if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
        if not await native_worker.run(
            lambda: native_adapter.is_element_valid(element),
            budget=operation_budget,
            operation="native element validity check",
        ):
            return f"ERROR: click [{element_id}]: element stale (UI changed)\n  -> try: tree to refresh"

    # Route CDP elements to CDP backend (unified: works for both stealth and existing browser)
    if element.source in {"cdp", "shadow-dom"} and element.platform_ref is not None:
        if snapshot is None:
            return f"ERROR: click [{element_id}]: STALE_SNAPSHOT: refresh web_tree"
        if snapshot.provider == "cdp-persistent":
            dispatched = False
            try:
                session = await _current_persistent_snapshot_session(
                    snapshot,
                    budget=operation_budget,
                )
                resolved = await operation_budget.wait_for(
                    session.send(
                        "DOM.resolveNode",
                        {"backendNodeId": element.platform_ref},
                        idempotent=True,
                    ),
                    operation="shadow element resolution",
                )
                object_id = resolved.get("object", {}).get("objectId")
                current = cdp_pool.get_session_for_target(snapshot.target_id)
                if (
                    not object_id
                    or current is not session
                    or current.generation != snapshot.generation
                ):
                    raise OperationError(
                        OperationErrorCode.STALE_SNAPSHOT,
                        "shadow element or target changed",
                    )
                await _assert_persistent_snapshot_current(
                    session,
                    snapshot,
                    budget=operation_budget,
                )
                dispatched = True
                click_result = await operation_budget.wait_for(
                    session.send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": (
                                "function() { if (!this.isConnected) "
                                "throw new Error('STALE_ELEMENT'); this.click(); }"
                            ),
                            "objectId": object_id,
                        },
                    ),
                    operation="shadow click",
                )
                if _persistent_runtime_exception(click_result) is not None:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                    if _persistent_runtime_exception_is_stale(click_result):
                        return (
                            f"ERROR: click [{element_id}]: STALE_SNAPSHOT: "
                            "shadow element disconnected before click"
                        )
                    return (
                        f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow click ended with a runtime exception"
                    )
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return f'clicked [{element_id}] {element.role} "{element.name}"'
            except OperationError as exc:
                if dispatched:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                    return (
                        f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow click may have been applied"
                    )
                return f"ERROR: click [{element_id}]: {exc}"
            except Exception as exc:
                logger.debug(
                    "Persistent shadow click failed (%s)",
                    type(exc).__name__,
                )
                if dispatched:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                    return (
                        f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow click may have been applied"
                    )
                return f"ERROR: click [{element_id}]: shadow provider failed"

        if snapshot.provider == "cdp-legacy":
            err = await _ensure_tabs(force=True, budget=operation_budget)
            if err:
                return err
            tab = next(
                (
                    candidate
                    for candidate in _cached_tabs
                    if candidate.id == snapshot.target_id
                ),
                None,
            )
            if tab is None:
                return f"ERROR: click [{element_id}]: STALE_SNAPSHOT: target closed"
            try:
                success = await operation_budget.wait_for(
                    cdp_client.click_element(
                        tab,
                        element.platform_ref,
                        expected_revision=snapshot.revision,
                    ),
                    operation="legacy shadow click",
                )
            except CDPDocumentChangedError:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return f"ERROR: click [{element_id}]: STALE_SNAPSHOT: page changed"
            except Exception as exc:
                logger.debug("Legacy shadow click failed (%s)", type(exc).__name__)
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return (
                    f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: "
                    "shadow click may have been applied"
                )
            if success:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return f'clicked [{element_id}] {element.role} "{element.name}"'
            return f"ERROR: Could not click [{element_id}] via shadow provider."

        return f"ERROR: click [{element_id}]: unsupported snapshot provider"

    # Native path for non-CDP elements
    if not native_adapter:
        return "ERROR: No native adapter available."

    # Strategy 1: AX action (reliable for most UI elements)
    for action in ("press", "click", "confirm", "open"):
        try:
            clicked = await _run_native_mutation(
                lambda candidate=action: native_adapter.perform_action(
                    element,
                    candidate,
                ),
                budget=operation_budget,
                operation=f"native {action} action",
                worker=native_worker,
            )
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(
                snapshot=snapshot,
                pid=element.pid,
            )
            return (
                f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: the native action "
                "may have been applied; refresh the target before retrying"
            )
        if clicked:
            return f'clicked [{element_id}] {element.role} "{element.name}"'

    # Strategy 2: Coordinate-based click (human-like fallback)
    # Activate the correct app first so the click goes to the right window.
    if element.bounds:
        input_backend = _input_backend
        if input_backend is not None and await input_worker.run(
            input_backend.is_available,
            budget=operation_budget,
            operation="coordinate fallback capability check",
        ):
            if element.pid:
                focus_ok, focus_err = await _verify_focus(
                    element.pid,
                    budget=operation_budget,
                )
                if not focus_ok:
                    return f"ERROR: {focus_err}. Click aborted to prevent wrong target."
            x, y, w, h = element.bounds
            cx, cy = x + w // 2, y + h // 2
            try:
                clicked = await _run_native_mutation(
                    lambda: input_backend.click(cx, cy),
                    budget=operation_budget,
                    operation="coordinate fallback click",
                    worker=input_worker,
                )
            except _NativeMutationOutcomeUnknown:
                _invalidate_native_mutation_state(
                    snapshot=snapshot,
                    pid=element.pid,
                )
                return (
                    f"ERROR: click [{element_id}]: OUTCOME_UNKNOWN: the coordinate "
                    "click may have been applied; refresh the target before retrying"
                )
            if clicked:
                return f'clicked [{element_id}] {element.role} "{element.name}"'

    return f"ERROR: Could not click [{element_id}]. Available actions: {element.actions}"


async def _handle_type(args: dict) -> str:
    budget = OperationBudget.start(5.0)
    element_id = args.get("id")
    text = args.get("text", "")
    element, snapshot, mode, action_key, error = _resolve_action_element(args, "type")
    if error:
        return error
    async def operation():
        return await _handle_type_resolved(
            element_id,
            text,
            element,
            snapshot=snapshot,
            budget=budget,
        )
    if mode is OperationMode.SHADOW:
        return await coordinator.execute_shadow(
            action_key,
            operation,
            budget=budget,
            operation_manages_deadline=True,
        )
    return await coordinator.execute_foreground(
        operation,
        budget=budget,
        operation_manages_deadline=True,
    )


def _typed_result(element_id: int, text: str) -> str:
    """Confirm input without reflecting potentially sensitive text."""
    return f"typed {len(text)} characters into [{element_id}]"


def _typed_verification_warning(element_id: int, text: str) -> str:
    """Report a mismatch without exposing requested or observed field values."""
    return (
        f"WARNING: dispatched {len(text)} characters to [{element_id}], but "
        "value verification did not match. The element may transform or reject input."
    )


async def _handle_type_resolved(
    element_id: int,
    text: str,
    element: UIElement,
    *,
    snapshot: ObservationSnapshot | None = None,
    budget: OperationBudget | None = None,
) -> str:
    operation_budget = budget or OperationBudget.start(5.0)

    def outcome_unknown() -> str:
        _invalidate_native_mutation_state(
            snapshot=snapshot,
            pid=element.pid,
        )
        return (
            f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: native input may have "
            "been applied; refresh the target before retrying"
        )

    # Validate element reference is still alive (app may have navigated, window closed)
    if hasattr(native_adapter, 'is_element_valid') and element.source == "native":
        if not await native_worker.run(
            lambda: native_adapter.is_element_valid(element),
            budget=operation_budget,
            operation="native element validity check",
        ):
            return f"ERROR: type [{element_id}]: element stale (UI changed)\n  -> try: tree to refresh"

    # Route shadow elements only through their originating target snapshot.
    if element.source in {"cdp", "shadow-dom"} and element.platform_ref is not None:
        if snapshot is None:
            return f"ERROR: type [{element_id}]: STALE_SNAPSHOT: refresh web_tree"
        if snapshot.provider == "cdp-persistent":
            focus_dispatched = False
            focus_acknowledged = False
            text_dispatched = False
            text_acknowledged = False
            try:
                session = await _current_persistent_snapshot_session(
                    snapshot,
                    budget=operation_budget,
                )
                resolved = await operation_budget.wait_for(
                    session.send(
                        "DOM.resolveNode",
                        {"backendNodeId": element.platform_ref},
                        idempotent=True,
                    ),
                    operation="shadow element resolution",
                )
                object_id = resolved.get("object", {}).get("objectId")
                current = cdp_pool.get_session_for_target(snapshot.target_id)
                if (
                    not object_id
                    or current is not session
                    or current.generation != snapshot.generation
                ):
                    raise OperationError(
                        OperationErrorCode.STALE_SNAPSHOT,
                        "shadow element or target changed",
                    )
                await _assert_persistent_snapshot_current(
                    session,
                    snapshot,
                    budget=operation_budget,
                )
                focus_dispatched = True
                focus_result = await operation_budget.wait_for(
                    session.send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": (
                                "function() { if (!this.isConnected) "
                                "throw new Error('STALE_ELEMENT'); this.focus(); }"
                            ),
                            "objectId": object_id,
                        },
                    ),
                    operation="shadow element focus",
                )
                focus_acknowledged = True
                if _persistent_runtime_exception(focus_result) is not None:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                    if _persistent_runtime_exception_is_stale(focus_result):
                        return (
                            f"ERROR: type [{element_id}]: STALE_SNAPSHOT: "
                            "shadow element disconnected; text was not sent"
                        )
                    return (
                        f"ERROR: type [{element_id}]: shadow focus failed; "
                        "text was not sent"
                    )
                await _assert_persistent_snapshot_current(
                    session,
                    snapshot,
                    budget=operation_budget,
                )
                text_dispatched = True
                await operation_budget.wait_for(
                    session.send("Input.insertText", {"text": text}),
                    operation="shadow text input",
                )
                text_acknowledged = True
                value_result = await operation_budget.wait_for(
                    session.send(
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": (
                                "function() { return this.value || "
                                "this.textContent || ''; }"
                            ),
                            "objectId": object_id,
                            "returnByValue": True,
                        },
                    ),
                    operation="shadow input verification",
                )
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                if _persistent_runtime_exception(value_result) is not None:
                    return _typed_verification_warning(element_id, text)
                actual_value = value_result.get("result", {}).get("value")
                if actual_value is not None and text in str(actual_value):
                    return _typed_result(element_id, text)
                if actual_value is not None:
                    return _typed_verification_warning(element_id, text)
                return _typed_result(element_id, text)
            except OperationError as exc:
                if focus_dispatched:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                if text_acknowledged:
                    return _typed_verification_warning(element_id, text)
                if text_dispatched:
                    return (
                        f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow input may have been applied"
                    )
                if focus_acknowledged:
                    return f"ERROR: type [{element_id}]: {exc}; text was not sent"
                if focus_dispatched:
                    return (
                        f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow focus may have changed; text was not sent"
                    )
                return f"ERROR: type [{element_id}]: {exc}"
            except Exception as exc:
                logger.debug(
                    "Persistent type provider failed with %s",
                    type(exc).__name__,
                )
                if focus_dispatched:
                    _invalidate_shadow_observation(
                        snapshot.provider,
                        snapshot.target_id,
                    )
                if text_acknowledged:
                    return _typed_verification_warning(element_id, text)
                if text_dispatched:
                    return (
                        f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow input may have been applied"
                    )
                if focus_acknowledged:
                    return (
                        f"ERROR: type [{element_id}]: shadow provider failed after "
                        "focus; text was not sent"
                    )
                if focus_dispatched:
                    return (
                        f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: "
                        "shadow focus may have changed; text was not sent"
                    )
                return f"ERROR: type [{element_id}]: shadow provider failed"

        if snapshot.provider == "cdp-legacy":
            err = await _ensure_tabs(force=True, budget=operation_budget)
            if err:
                return err
            tab = next(
                (
                    candidate
                    for candidate in _cached_tabs
                    if candidate.id == snapshot.target_id
                ),
                None,
            )
            if tab is None:
                return f"ERROR: type [{element_id}]: STALE_SNAPSHOT: target closed"
            try:
                success = await operation_budget.wait_for(
                    cdp_client.type_text(
                        tab,
                        element.platform_ref,
                        text,
                        expected_revision=snapshot.revision,
                    ),
                    operation="legacy shadow text input",
                )
            except CDPDocumentChangedError:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return f"ERROR: type [{element_id}]: STALE_SNAPSHOT: page changed"
            except Exception as exc:
                logger.debug("Legacy shadow type failed (%s)", type(exc).__name__)
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return (
                    f"ERROR: type [{element_id}]: OUTCOME_UNKNOWN: "
                    "shadow input may have been applied"
                )
            if success:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                try:
                    actual_value = await operation_budget.wait_for(
                        cdp_client.get_element_value(tab, element.platform_ref),
                        operation="legacy shadow input verification",
                    )
                except Exception as exc:
                    logger.debug(
                        "Legacy shadow input verification failed after acknowledged "
                        "input (%s)",
                        type(exc).__name__,
                    )
                    return _typed_result(element_id, text)
                if actual_value is not None and text in actual_value:
                    return _typed_result(element_id, text)
                if actual_value is not None:
                    return _typed_verification_warning(element_id, text)
                return _typed_result(element_id, text)
            return f"ERROR: Could not type into [{element_id}] via CDP."

        return f"ERROR: type [{element_id}]: unsupported snapshot provider"

    # Native path for non-CDP elements
    if not native_adapter:
        return "ERROR: No native adapter available."

    input_backend = _input_backend
    input_available = bool(
        input_backend is not None
        and await input_worker.run(
            input_backend.is_available,
            budget=operation_budget,
            operation="native type capability check",
        )
    )
    is_web = "scrolltovisible" in element.actions  # web elements have this action

    def exact_element_is_focused() -> bool:
        focused_element = native_adapter.get_focused_element()
        return bool(
            focused_element is not None
            and native_adapter.is_same_element(element, focused_element)
        )

    # ── Step 1: Activate the target app window (always, for all strategies)
    if element.pid and input_available:
        focus_ok, focus_err = await _verify_focus(
            element.pid,
            budget=operation_budget,
        )
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
    if not is_secure and hasattr(native_adapter, 'focus_element') and input_available:
        focus_state = ProviderCallState()
        try:
            focus_result = await run_native_action_until(
                element.pid,
                lambda: native_adapter.focus_element(element),
                exact_element_is_focused,
                budget=operation_budget,
                action_worker=native_worker,
                condition_worker=native_worker,
                action_state=focus_state,
            )
        except (OperationError, asyncio.CancelledError):
            if focus_state.may_have_run:
                coordinator.poison_foreground_until(native_worker.wait_until_idle())
                return outcome_unknown()
            raise
        focused = bool(focus_result.condition_met)
        if focus_result.action_result and not focused:
            return (
                f"ERROR: type [{element_id}]: FOCUS_MISMATCH: exact target element "
                "did not receive focus; text was not sent"
            )
        if focused:
            # For web elements: type directly WITHOUT clear_and_type.
            # clear_and_type sends Cmd+A + Delete before typing, which in Chrome
            # can select the entire page, trigger shortcuts (Cmd+T = new tab), and
            # cause havoc when the contentEditable div doesn't have perfect focus.
            try:
                if is_web:
                    type_ok = await _run_native_mutation(
                        lambda: input_backend.type_text(text),
                        budget=operation_budget,
                        operation="native bulk text input",
                        worker=input_worker,
                    )
                else:
                    type_ok = await _run_native_mutation(
                        lambda: input_backend.clear_and_type(text),
                        budget=operation_budget,
                        operation="native clear and bulk text input",
                        worker=input_worker,
                    )
            except _NativeMutationOutcomeUnknown:
                return outcome_unknown()
            if type_ok:
                # For web elements: trust keyboard injection, skip verification.
                if is_web:
                    return _typed_result(element_id, text)

                # For native elements: verify the text actually landed.
                # Some apps (e.g. Jamf, secure input) silently ignore CGEvent
                # keystrokes while AX set_value works.
                verified = False
                if element.platform_ref and hasattr(native_adapter, '_read_attr'):
                    current_val = await native_worker.run(
                        lambda: native_adapter._read_attr(
                            element.platform_ref,
                            "AXValue",
                        ),
                        budget=operation_budget,
                        operation="native typed value verification",
                    )
                    if current_val and text in str(current_val):
                        verified = True
                    elif current_val is None:
                        # Can't verify (field doesn't expose value) — assume success
                        verified = True
                else:
                    verified = True  # No way to verify, assume success

                if verified:
                    return _typed_result(element_id, text)
                # Keyboard injection didn't land — mark it and fall through to set_value
                keyboard_injected = True

    # ── Strategy 2: Coordinate click + type (when focus_element fails)
    # Skip if keyboard injection already tried and failed (same mechanism, same result)
    if not keyboard_injected and input_available and element.bounds:
        x, y, w, h = element.bounds
        cx, cy = x + w // 2, y + h // 2

        def exact_element_at_position() -> UIElement | None:
            hit = native_adapter.element_at_position(cx, cy)
            if hit is None or not native_adapter.is_same_element(element, hit):
                return None
            return hit

        hit = await native_worker.run(
            exact_element_at_position,
            budget=operation_budget,
            operation="native coordinate target verification",
        )
        if hit is None:
            return (
                f"ERROR: type [{element_id}]: FOCUS_MISMATCH: target bounds no "
                "longer identify the exact element; text was not sent"
            )
        click_state = ProviderCallState()
        try:
            click_result = await run_native_action_until(
                element.pid,
                lambda: input_backend.click(cx, cy),
                exact_element_is_focused,
                budget=operation_budget,
                action_worker=input_worker,
                condition_worker=native_worker,
                action_state=click_state,
            )
        except (OperationError, asyncio.CancelledError):
            if click_state.may_have_run:
                coordinator.poison_foreground_until(input_worker.wait_until_idle())
                return outcome_unknown()
            raise
        if not click_result.condition_met:
            return (
                f"ERROR: type [{element_id}]: FOCUS_MISMATCH: click did not focus "
                "the exact target element; text was not sent"
            )
        try:
            type_ok = await _run_native_mutation(
                lambda: input_backend.clear_and_type(text),
                budget=operation_budget,
                operation="native verified coordinate text input",
                worker=input_worker,
            )
        except _NativeMutationOutcomeUnknown:
            return outcome_unknown()
        if type_ok:
            # Verify text landed
            verified = False
            if element.platform_ref and hasattr(native_adapter, '_read_attr'):
                current_val = await native_worker.run(
                    lambda: native_adapter._read_attr(
                        element.platform_ref,
                        "AXValue",
                    ),
                    budget=operation_budget,
                    operation="native typed value verification",
                )
                if current_val and text in str(current_val):
                    verified = True
                elif current_val is None:
                    verified = True
            else:
                verified = True

            if verified:
                return _typed_result(element_id, text)

    # ── Strategy 3: set_value + AXConfirm (fallback for apps that block keyboard injection)
    # Used when keyboard injection fails verification, OR when no input backend available.
    # Works for Jamf Connect, security apps, and apps with secure/custom text fields.
    try:
        value_set = await _run_native_mutation(
            lambda: native_adapter.set_value(element, text),
            budget=operation_budget,
            operation="native accessibility value set",
            worker=native_worker,
        )
    except _NativeMutationOutcomeUnknown:
        return outcome_unknown()
    if value_set:
        if element.platform_ref:
            try:
                await _run_native_mutation(
                    lambda: native_adapter.perform_action(element, "confirm"),
                    budget=operation_budget,
                    operation="native value confirmation",
                    worker=native_worker,
                )
            except _NativeMutationOutcomeUnknown:
                return outcome_unknown()
        return _typed_result(element_id, text)

    return f"ERROR: Could not type into [{element_id}]. Element may not be editable."


def _handle_get_focused() -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    element = native_adapter.get_focused_element()
    if element is None:
        return "No focused element found."

    registry.register_element(element)
    return f"Focused element:\n{element.to_text(max_depth=2)}"


async def _handle_get_focused_async() -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    element = await native_worker.run(
        native_adapter.get_focused_element,
        budget=OperationBudget.start(5.0),
        operation="focused element query",
    )
    if element is None:
        return "No focused element found."

    registry.register_element(element)
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id=f"pid:{element.pid}",
        generation=0,
        revision=time.monotonic_ns(),
        elements=[ElementRecord(local_id=element.id, value=element)],
    )
    return f"snapshot={snapshot.token}\nFocused element:\n{element.to_text(max_depth=0)}"


# ── CDP handlers ────────────────────────────────────────────────────
# _cached_tabs_time removed — Tier 2 (cdp_pool) tracks tabs via auto-attach;
# Tier 3 (_ensure_tabs) uses its own 30-second cache window internally.
_cached_tabs: list = []
_tabs_lock = asyncio.Lock()  # Protects _cached_tabs from concurrent mutation
_native_target_cache: dict[str, object] = {}
_NATIVE_BROWSER_INVENTORY_FLIGHT = "native-browser-inventory"


async def _collect_native_browser_targets(
    *,
    budget: OperationBudget,
) -> list:
    """Coalesce concurrent full accessibility scans into one provider call."""

    async def collect() -> list:
        return await native_worker.run(
            lambda: collect_browser_targets(native_adapter),
            budget=OperationBudget.start(5.0),
            operation="foreground browser inventory",
        )

    return await coordinator.observe(
        (_NATIVE_BROWSER_INVENTORY_FLIGHT, id(native_adapter)),
        collect,
        budget=budget,
    )


def _invalidate_native_mutation_state(
    *,
    snapshot: ObservationSnapshot | None = None,
    pid: int | None = None,
) -> None:
    """Discard native observations whose UI state may have changed."""
    registry.clear()
    _native_target_cache.clear()
    if snapshot is not None and snapshot.provider == "native":
        coordinator.observations.invalidate_target(
            provider=snapshot.provider,
            mode=snapshot.mode,
            target_id=snapshot.target_id,
        )
    elif pid:
        coordinator.observations.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=f"pid:{pid}",
        )
    else:
        coordinator.observations.invalidate_provider(
            provider="native",
            mode=OperationMode.FOREGROUND,
        )


async def _get_cdp_session(
    args: dict,
    *,
    budget: OperationBudget | None = None,
) -> tuple:
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
    target_id = str(args.get("target_id", "")).strip()
    if target_id.startswith("apple-events:"):
        return None, None, ""
    operation_budget = budget or OperationBudget.start(5.0)

    # ── Tier 1: persistent WebSocket ──
    try:
        await operation_budget.wait_for(
            cdp_pool.ensure_connected(),
            operation="persistent shadow provider discovery",
        )
        pool_tabs = cdp_pool.list_tabs()
        if pool_tabs:
            if target_id:
                target_tab = next(
                    (candidate for candidate in pool_tabs if candidate.id == target_id),
                    None,
                )
                session = cdp_pool.get_session_for_target(target_id)
                if target_tab is not None and session is not None:
                    tier_manager.set_available(ConnectionTier.CDP, True)
                    return session, target_tab, ""
            elif isinstance(tab_index, int) and 0 <= tab_index < len(pool_tabs):
                session = cdp_pool.get_session_for_tab(tab_index)
                if session is not None:
                    tier_manager.set_available(ConnectionTier.CDP, True)
                    return session, pool_tabs[tab_index], ""
    except Exception as exc:
        logger.debug(
            "Persistent shadow session unavailable (%s)",
            type(exc).__name__,
        )

    # ── Tier 2 fallback: legacy cdp_client ──
    # _ensure_tabs / cdp_client are still used for all other handlers
    err = await operation_budget.wait_for(
        _ensure_tabs(budget=operation_budget),
        operation="legacy shadow provider discovery",
    )
    if not err:
        if target_id:
            tab = next(
                (candidate for candidate in _cached_tabs if candidate.id == target_id),
                None,
            )
            if tab is None:
                return None, None, "ERROR: Requested shadow target is unavailable."
            err = ""
        else:
            tab, err = _get_tab(args)
        if not err:
            return None, tab, ""
        return None, None, err

    return None, None, err


async def _persistent_document_revision(session) -> int:
    """Return a deterministic revision for the current top-level document."""
    import hashlib

    frame_result = await session.send("Page.getFrameTree", idempotent=True)
    document_result = await session.send(
        "DOM.getDocument",
        {"depth": 0, "pierce": False},
        idempotent=True,
    )
    frame = frame_result.get("frameTree", {}).get("frame", {})
    frame_identity = frame.get("loaderId") or frame.get("id")
    root_identity = document_result.get("root", {}).get("backendNodeId")
    if (
        not isinstance(frame_identity, str)
        or not frame_identity
        or isinstance(root_identity, bool)
        or not isinstance(root_identity, int)
        or root_identity <= 0
    ):
        raise RuntimeError("CDP page did not expose a document revision")
    identity = f"{frame_identity}:{root_identity}"
    digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _snapshot_shadow_tree(
    tree: UIElement,
    *,
    provider: str,
    target_id: str,
    generation: int,
    revision: int,
) -> tuple[str, int, frozenset[int]]:
    """Bind one CDP tree to an immutable provider target state."""
    records: list[ElementRecord] = []
    pending = [tree]
    while pending and len(records) < 500:
        element = pending.pop()
        backend_id = element.platform_ref
        actionable = (
            element.source in {"cdp", "shadow-dom"}
            and isinstance(backend_id, int)
            and not isinstance(backend_id, bool)
            and backend_id > 0
        )
        records.append(
            ElementRecord(
                local_id=element.id,
                value=element,
                actionable=actionable,
            )
        )
        pending.extend(reversed(element.children))
    snapshot = coordinator.observations.create(
        provider=provider,
        mode=OperationMode.SHADOW,
        target_id=target_id,
        generation=generation,
        revision=revision,
        elements=records,
    )
    return snapshot.token, len(records), frozenset(record.local_id for record in records)


async def _current_persistent_snapshot_session(
    snapshot: ObservationSnapshot,
    *,
    budget: OperationBudget,
):
    """Resolve and verify one exact persistent-CDP snapshot target."""
    session = cdp_pool.get_session_for_target(snapshot.target_id)
    if session is None or session.generation != snapshot.generation:
        raise OperationError(
            OperationErrorCode.STALE_SNAPSHOT,
            "shadow target connection generation changed",
        )
    await _assert_persistent_snapshot_current(
        session,
        snapshot,
        budget=budget,
    )
    return session


async def _assert_persistent_snapshot_current(
    session,
    snapshot: ObservationSnapshot,
    *,
    budget: OperationBudget,
) -> None:
    """Fail closed unless target generation and document revision still match."""
    current = cdp_pool.get_session_for_target(snapshot.target_id)
    if current is not session or current.generation != snapshot.generation:
        raise OperationError(
            OperationErrorCode.STALE_SNAPSHOT,
            "shadow target connection changed during validation",
        )
    revision = await budget.wait_for(
        _persistent_document_revision(session),
        operation="shadow document revision check",
    )
    if revision != snapshot.revision:
        raise OperationError(
            OperationErrorCode.STALE_SNAPSHOT,
            "shadow target document changed",
        )
    current = cdp_pool.get_session_for_target(snapshot.target_id)
    if current is not session or current.generation != snapshot.generation:
        raise OperationError(
            OperationErrorCode.STALE_SNAPSHOT,
            "shadow target connection changed during validation",
        )


async def _collect_explicit_shadow_tabs() -> tuple[list, str]:
    """Return the first usable explicitly requested shadow inventory."""
    if cdp_pool.is_connected:
        tabs = cdp_pool.list_tabs()
        if tabs:
            return tabs, "persistent"

    async def persistent_tabs() -> list:
        budget = OperationBudget.start(5.0)
        await budget.wait_for(
            cdp_pool.ensure_connected(),
            operation="persistent shadow tab inventory",
        )
        return cdp_pool.list_tabs() if cdp_pool.is_connected else []

    async def legacy_tabs() -> list:
        budget = OperationBudget.start(5.0)
        available = await budget.wait_for(
            cdp_client.is_available(),
            operation="legacy shadow capability check",
        )
        if not available:
            return []
        return await budget.wait_for(
            cdp_client.list_tabs(),
            operation="legacy shadow tab inventory",
        )

    async def apple_tabs() -> list:
        return await apple_worker.run(
            lambda: _get_applescript_tabs(force=True),
            budget=OperationBudget.start(5.0),
            operation="Apple Events tab inventory",
        )

    provider_tasks = {
        "persistent": asyncio.create_task(persistent_tabs()),
        "legacy": asyncio.create_task(legacy_tabs()),
    }
    if sys.platform == "darwin" and _as is not None:
        provider_tasks["apple-events"] = asyncio.create_task(apple_tabs())

    pending = set(provider_tasks.values())
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for provider in ("persistent", "legacy", "apple-events"):
                task = provider_tasks.get(provider)
                if task not in done:
                    continue
                try:
                    tabs = task.result()
                except Exception as exc:
                    logger.debug(
                        "Explicit %s tab inventory unavailable (%s)",
                        provider,
                        type(exc).__name__,
                    )
                    continue
                if tabs:
                    return tabs, provider
        return [], ""
    finally:
        for task in provider_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*provider_tasks.values(), return_exceptions=True)


async def _handle_list_tabs(args: dict) -> str:
    """List foreground browser state; CDP is an explicit shadow-only add-on."""
    global _cached_tabs, _native_target_cache

    query = str(args.get("query", "")).strip()
    max_results = max(1, min(int(args.get("max_results", 10)), 50))
    shadow_requested = bool(args.get("shadow", False))

    if native_adapter is None and not shadow_requested:
        return "ERROR: The foreground native accessibility provider is unavailable."

    async def collect_optional_native_targets() -> list:
        try:
            return await _collect_native_browser_targets(
                budget=OperationBudget.start(5.0)
            )
        except Exception as exc:
            logger.debug(
                "Foreground inventory unavailable during explicit shadow listing (%s)",
                type(exc).__name__,
            )
            return []

    shadow_tabs = []
    shadow_provider = ""
    if shadow_requested:
        if native_adapter is None:
            shadow_tabs, shadow_provider = await _collect_explicit_shadow_tabs()
            native_targets = []
        else:
            native_targets, shadow_result = await asyncio.gather(
                collect_optional_native_targets(),
                _collect_explicit_shadow_tabs(),
            )
            shadow_tabs, shadow_provider = shadow_result
    else:
        native_targets = await _collect_native_browser_targets(
            budget=OperationBudget.start(5.0)
        )

    _native_target_cache = {target.identifier: target for target in native_targets}
    native_result = format_browser_targets(
        native_targets,
        query=query,
        max_query_results=max_results,
    )

    if not shadow_requested:
        return native_result

    if shadow_tabs:
        # Persistent sessions own their own live target inventory. Mixing those
        # descriptors into the legacy cache can later route a per-request CDP
        # mutation through a WebSocket URL from a different provider.
        if shadow_provider == "legacy":
            async with _tabs_lock:
                _cached_tabs = list(shadow_tabs)
        shadow_lines = ["Explicit shadow-provider tabs:"]
        shown_shadow_tabs = list(shadow_tabs[:max_results])
        for index, tab in enumerate(shown_shadow_tabs):
            title = " ".join(tab.title.split())[:160]
            url = sanitize_url_for_display(tab.url)[:180]
            target_id = str(
                tab.identifier if shadow_provider == "apple-events" else tab.id
            )[:512]
            shadow_lines.append(
                f"[shadow:{index}] target_id={target_id} {title} — {url}"
            )
        if len(shadow_tabs) > len(shown_shadow_tabs):
            shadow_lines.append(
                f"{len(shadow_tabs) - len(shown_shadow_tabs)} additional shadow targets omitted."
            )
        shadow_lines.append(
            "Use target_id for exact shadow targeting."
        )
        return f"{native_result}\n\n" + "\n".join(shadow_lines)

    return (
        f"{native_result}\n\n"
        "The optional shadow provider is not connected. Foreground browser control remains available."
    )


async def _handle_list_chrome_tabs() -> str:
    """Backward-compatible private alias for older in-process integrations."""
    return await _handle_list_tabs({})


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


async def _append_shadow_dom_elements(
    tree: UIElement,
    tab,
    chrome_pid: int,
    tab_index: int,
    page_url: str,
    *,
    register: bool = True,
) -> int:
    """Augment the accessibility tree with shadow DOM elements from CDP pierce mode.

    Calls DOM.getFlattenedDocument(pierce=True), finds elements that are NOT
    already in the AX tree (by backendDOMNodeId), converts them to UIElement
    objects, appends them as a flat list of children to the tree root, and
    registers them in the registry.

    Returns the count of shadow DOM elements added.
    This is a best-effort operation — any failure is silently ignored so the
    standard tree is always returned intact.
    """
    try:
        pierced_nodes = await asyncio.wait_for(
            cdp_client.get_pierced_dom(tab), timeout=3.0
        )
        if not pierced_nodes:
            return 0

        return _attach_shadow_dom_nodes(
            tree,
            pierced_nodes,
            chrome_pid=chrome_pid,
            tab_index=tab_index,
            register=register,
        )
    except Exception as exc:
        logger.debug(
            "Legacy shadow DOM augmentation failed (%s)",
            type(exc).__name__,
        )
        return 0


async def _append_persistent_shadow_dom_elements(
    tree: UIElement,
    session,
    chrome_pid: int,
    tab_index: int,
) -> int:
    """Augment a tree through the already-bound persistent target session."""
    try:
        pierced_nodes = await session.get_pierced_dom()
        return _attach_shadow_dom_nodes(
            tree,
            pierced_nodes,
            chrome_pid=chrome_pid,
            tab_index=tab_index,
        )
    except Exception as exc:
        logger.debug(
            "Persistent shadow DOM augmentation failed (%s)",
            type(exc).__name__,
        )
        return 0


def _attach_shadow_dom_nodes(
    tree: UIElement,
    pierced_nodes: list[dict],
    *,
    chrome_pid: int,
    tab_index: int,
    register: bool = True,
) -> int:
    """Attach pierced nodes while preserving canonical backend node IDs."""
    existing_ids: set[int] = set()
    _collect_platform_refs(tree, existing_ids)
    shadow_dicts = merge_pierced_nodes(pierced_nodes, existing_ids)
    shadow_elements: list[UIElement] = []
    for shadow in shadow_dicts:
        backend_id = shadow.get("backendNodeId")
        actionable = bool(shadow.get("actionable", False))
        element = UIElement(
            id=cdp_client._next_id(),
            role=shadow["role"],
            name=shadow["name"],
            states=[] if actionable else ["read-only"],
            actions=["click", "focus"] if actionable else [],
            source="shadow-dom",
            platform_ref=backend_id,
            pid=chrome_pid,
            tab_index=tab_index,
        )
        shadow_elements.append(element)
    if register:
        registry.register_elements(shadow_elements)
    tree.children.extend(shadow_elements)
    return len(shadow_elements)


def _collect_platform_refs(element: UIElement, refs: set[int]) -> None:
    """Recursively collect all platform_ref values from a UIElement tree."""
    if element.platform_ref is not None:
        refs.add(element.platform_ref)
    for child in element.children:
        _collect_platform_refs(child, refs)


def _format_web_tree_response(
    tree: UIElement,
    tab_title: str,
    tab_url: str,
    tab_index: int,
    max_depth: int,
    interactive_only: bool,
    snapshot_token: str,
    snapshot_count: int,
    snapshot_ids: frozenset[int],
    suffix: str = "",
    action_hint: str = "Use [id] numbers with click or type to interact.",
) -> str:
    """Render a web tree as either interactive flat-lines or full nested text."""
    if interactive_only:
        elements: list[UIElement] = []
        _collect_interactive_flat(
            tree,
            elements,
            _BASE_INTERACTIVE_ROLES,
            allowed_ids=snapshot_ids,
        )
        if not elements:
            return (
                f"snapshot={snapshot_token}\n"
                f"No interactive elements found in tab {tab_index}: {tab_title}\n"
                "Use full=True for the complete tree."
            )
        lines = [el.to_flat_line() for el in elements]
        return (
            f"snapshot={snapshot_token}\n"
            f"{suffix}"
            + "\n".join(lines)
        )

    # Full nested format
    text = _render_snapshot_tree(tree, snapshot_ids, max_depth=max_depth)
    return (
        f"snapshot={snapshot_token}\n"
        f"Web accessibility tree for: {tab_title}\n"
        f"URL: {sanitize_url_for_display(tab_url)}\n"
        f"Elements: {snapshot_count}\n"
        f"{suffix}"
        f"\n{text}\n\n"
        f"{action_hint}"
    )


def _convert_js_ax_tree(payload: object) -> UIElement | None:
    """Convert the bounded Apple Events DOM observation into read-only elements."""
    seen: set[int] = set()
    remaining = 500

    def convert(node: object) -> UIElement | None:
        nonlocal remaining
        if remaining <= 0 or not isinstance(node, dict):
            return None
        local_id = node.get("id")
        if (
            isinstance(local_id, bool)
            or not isinstance(local_id, int)
            or local_id < 0
            or local_id in seen
        ):
            return None
        seen.add(local_id)
        remaining -= 1

        raw_bounds = node.get("bounds")
        bounds = None
        if (
            isinstance(raw_bounds, list)
            and len(raw_bounds) == 4
            and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in raw_bounds
            )
        ):
            bounds = tuple(int(value) for value in raw_bounds)

        states = ["read-only"]
        if node.get("secure"):
            states.append("secure")
        if node.get("disabled"):
            states.append("disabled")
        if node.get("checked") is True:
            states.append("checked")
        elif node.get("checked") is False:
            states.append("unchecked")
        if node.get("expanded") is True:
            states.append("expanded")
        elif node.get("expanded") is False:
            states.append("collapsed")

        element = UIElement(
            id=local_id,
            role=str(node.get("role", ""))[:120],
            name=str(node.get("name", ""))[:200],
            value="" if node.get("secure") else str(node.get("value", ""))[:200],
            states=states,
            actions=[],
            bounds=bounds,
            source="apple-events",
        )
        children = node.get("children", [])
        if isinstance(children, list):
            for child in children:
                converted = convert(child)
                if converted is not None:
                    element.children.append(converted)
        return element

    return convert(payload)


async def _handle_get_web_tree(args: dict) -> str:
    if not args.get("shadow", False):
        return (
            "ERROR: MODE_MISMATCH: foreground browser inspection uses tree with "
            "the browser PID. "
            "Set shadow=true only when DOM/CDP background inspection was explicitly requested."
        )

    target_id, target_error = _require_shadow_target_id(args, "shadow web tree")
    if target_error:
        return target_error
    tab_index = args.get("tab_index", 0)
    max_depth = min(args.get("max_depth", 5), 10)
    full = args.get("full", False)
    interactive_only = args.get("interactive_only", True) and not full
    budget = OperationBudget.start(5.0)

    # Try Tier 2 (persistent CDP) then Tier 3 (legacy CDP)
    session, tab, cdp_err = await _get_cdp_session(args, budget=budget)

    if session is not None and tab is not None:
        # Persistent target: bracket acquisition with document revisions so an
        # old tree can never be bound to a newly navigated document.
        try:
            await budget.wait_for(
                session.enable_domain("Accessibility"),
                operation="shadow accessibility setup",
            )
            for attempt in range(2):
                revision_before = await budget.wait_for(
                    _persistent_document_revision(session),
                    operation="shadow document revision",
                )
                result = await budget.wait_for(
                    session.send(
                        "Accessibility.getFullAXTree",
                        {"depth": max_depth},
                        idempotent=True,
                    ),
                    operation="shadow accessibility tree",
                )
                nodes = result.get("nodes", [])
                if nodes:
                    async def send_secure_metadata(method: str, params: dict) -> dict:
                        return await session.send(
                            method,
                            params,
                            idempotent=True,
                        )

                    secure_metadata = await budget.wait_for(
                        cdp_client.collect_secure_dom_metadata(
                            send_secure_metadata,
                            nodes,
                        ),
                        operation="shadow secure-field classification",
                    )
                    tree = cdp_client._build_tree(
                        nodes,
                        secure_metadata=secure_metadata,
                    )
                    if tree is not None:
                        chrome_pid = await system_worker.run(
                            _get_chrome_pid,
                            budget=budget,
                            operation="browser process lookup",
                        )
                        registry.register_tree(tree, pid=chrome_pid, tab_index=tab_index, page_url=tab.url)
                        await budget.wait_for(
                            _append_persistent_shadow_dom_elements(
                                tree,
                                session,
                                chrome_pid,
                                tab_index,
                            ),
                            operation="shadow DOM augmentation",
                        )
                        revision_after = await budget.wait_for(
                            _persistent_document_revision(session),
                            operation="shadow document revision",
                        )
                        if revision_before != revision_after:
                            if attempt == 0:
                                continue
                            return (
                                "ERROR: Shadow document changed while it was being "
                                "observed; retry web_tree."
                            )
                        snapshot_token, snapshot_count, snapshot_ids = _snapshot_shadow_tree(
                            tree,
                            provider="cdp-persistent",
                            target_id=tab.id,
                            generation=session.generation,
                            revision=revision_after,
                        )
                        return _format_web_tree_response(
                            tree,
                            tab.title,
                            tab.url,
                            tab_index,
                            max_depth,
                            interactive_only,
                            snapshot_token,
                            snapshot_count,
                            snapshot_ids,
                        )
                break
            return "ERROR: No accessibility tree is available for the shadow target."
        except OperationError as exc:
            return f"ERROR: web_tree: {exc}"
        except Exception as exc:
            logger.debug(
                "Persistent web tree failed (%s)",
                type(exc).__name__,
            )
            return "ERROR: Persistent shadow web-tree provider failed."

    if target_id and not target_id.startswith("apple-events:") and tab is None:
        return cdp_err or "ERROR: Requested shadow target is unavailable."

    # Tier 3: exact-target legacy CDP. Bracket all tree acquisition and
    # optional DOM piercing with a deterministic document revision.
    if not target_id.startswith("apple-events:") and tab is not None:
        legacy_tab = tab
        try:
            for attempt in range(2):
                revision_before = await budget.wait_for(
                    cdp_client.get_document_revision(legacy_tab),
                    operation="legacy shadow document revision",
                )
                tree = await budget.wait_for(
                    cdp_client.get_accessibility_tree(legacy_tab, max_depth),
                    operation="legacy shadow accessibility tree",
                )
                if tree is None:
                    return "ERROR: No accessibility tree is available for the shadow target."
                chrome_pid = await system_worker.run(
                    _get_chrome_pid,
                    budget=budget,
                    operation="browser process lookup",
                )
                await budget.wait_for(
                    _append_shadow_dom_elements(
                        tree,
                        legacy_tab,
                        chrome_pid,
                        tab_index,
                        legacy_tab.url,
                        register=False,
                    ),
                    operation="legacy shadow DOM augmentation",
                )
                revision_after = await budget.wait_for(
                    cdp_client.get_document_revision(legacy_tab),
                    operation="legacy shadow document revision",
                )
                if revision_before != revision_after:
                    if attempt == 0:
                        continue
                    return (
                        "ERROR: Shadow document changed while it was being "
                        "observed; retry web_tree."
                    )
                registry.register_tree(
                    tree,
                    pid=chrome_pid,
                    tab_index=tab_index,
                    page_url=legacy_tab.url,
                )
                snapshot_token, snapshot_count, snapshot_ids = _snapshot_shadow_tree(
                    tree,
                    provider="cdp-legacy",
                    target_id=legacy_tab.id,
                    generation=0,
                    revision=revision_after,
                )
                return _format_web_tree_response(
                    tree,
                    legacy_tab.title,
                    legacy_tab.url,
                    tab_index,
                    max_depth,
                    interactive_only,
                    snapshot_token,
                    snapshot_count,
                    snapshot_ids,
                )
        except OperationError as exc:
            return f"ERROR: web_tree: {exc}"
        except Exception as exc:
            logger.debug("Legacy web tree failed (%s)", type(exc).__name__)
            return "ERROR: Legacy shadow web-tree provider failed."

    # Optional Apple Events observation for an explicitly selected stable tab.
    if (
        target_id.startswith("apple-events:")
        and sys.platform == "darwin"
        and _as is not None
    ):
        def load_apple_tree():
            import hashlib
            import json

            if not _as.is_available():
                return None, None, 0, ""
            apple_target, resolve_error = _resolve_applescript_target(target_id)
            if resolve_error:
                return None, None, 0, resolve_error
            raw = _as.execute_javascript(
                build_ax_tree_script(max_depth=max_depth),
                tab_index=apple_target.index,
                window_index=apple_target.window_index,
                tab_id=apple_target.id,
                window_id=apple_target.window_id,
            )
            if not raw or raw.startswith("ERROR:"):
                return apple_target, None, 0, ""
            parsed = json.loads(raw)
            converted = _convert_js_ax_tree(parsed)
            digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest()
            return apple_target, converted, int.from_bytes(digest, "big"), ""

        try:
            apple_target, tree, revision, apple_error = await apple_worker.run(
                load_apple_tree,
                budget=budget,
                operation="Apple Events web tree",
            )
        except Exception as exc:
            logger.debug("Apple Events web tree failed (%s)", type(exc).__name__)
            return "ERROR: Apple Events web-tree provider failed."
        if apple_error:
            return apple_error
        if apple_target is not None and tree is not None:
            registry.register_tree(
                tree,
                tab_index=apple_target.index,
                page_url=apple_target.url,
            )
            snapshot_token, snapshot_count, snapshot_ids = _snapshot_shadow_tree(
                tree,
                provider="apple-events",
                target_id=target_id,
                generation=0,
                revision=revision,
            )
            return _format_web_tree_response(
                tree,
                apple_target.title,
                apple_target.url,
                apple_target.index,
                max_depth,
                interactive_only,
                snapshot_token,
                snapshot_count,
                snapshot_ids,
                suffix=(
                    "Apple Events read-only observation; use the shadow selector/text "
                    "tool with this target_id for interaction.\n"
                ),
                action_hint=(
                    "These snapshot IDs are read-only; refresh web_tree after any "
                    "Apple Events mutation."
                ),
            )

    return cdp_err or "ERROR: The explicitly requested shadow web-tree provider is unavailable."


# ── CDP action handlers ─────────────────────────────────────────────

async def _ensure_tabs(
    force: bool = False,
    *,
    budget: OperationBudget | None = None,
) -> str:
    """Ensure cached tabs are available. Returns error string or empty.

    NOTE: This is the Tier 3 fallback only. Tier 2 (cdp_pool / PersistentCDP)
    tracks tabs automatically via Target.attachedToTarget and does not use
    this function. Only handlers that have not yet been migrated to _get_cdp_session
    should call _ensure_tabs directly.

    Args:
        force: Always refresh, ignoring cache age. Use when listing tabs explicitly.
    """
    global _cached_tabs
    operation_budget = budget or OperationBudget.start(5.0)
    acquired = False
    try:
        await operation_budget.wait_for(
            _tabs_lock.acquire(),
            operation="legacy tab-cache lock",
        )
        acquired = True
        if not force and _cached_tabs:
            return ""
        available = await operation_budget.wait_for(
            cdp_client.is_available(),
            operation="legacy shadow capability check",
        )
        if not available:
            return "ERROR: The optional shadow browser provider is unavailable."
        tabs = await operation_budget.wait_for(
            cdp_client.list_tabs(),
            operation="legacy shadow tab inventory",
        )
        _cached_tabs = list(tabs)
        if not _cached_tabs:
            return "ERROR: No tabs are exposed by the optional shadow browser provider."
        return ""
    finally:
        if acquired:
            _tabs_lock.release()


def _get_tab(args: dict) -> tuple:
    """Get tab by index from args. Returns (tab, error_string)."""
    idx = args.get("tab_index", 0)
    if not isinstance(idx, int) or idx < 0 or idx >= len(_cached_tabs):
        return None, f"ERROR: Tab index {idx} out of range. {len(_cached_tabs)} tab(s) available."
    return _cached_tabs[idx], ""


# ── AppleScript tab resolution ──────────────────────────────────────

_as_tabs_cache: list = []
_as_tabs_cache_time: float = 0.0
_AS_CACHE_TTL = 2.0  # seconds


def _get_applescript_tabs(force: bool = False) -> list:
    """Get AppleScript tab list with short-lived cache."""
    global _as_tabs_cache, _as_tabs_cache_time
    now = time.time()
    if not force and _as_tabs_cache and (now - _as_tabs_cache_time) < _AS_CACHE_TTL:
        return _as_tabs_cache
    if _as is not None and _as.is_available():
        _as_tabs_cache = _as.list_chrome_tabs()
        _as_tabs_cache_time = now
    return _as_tabs_cache


def _invalidate_applescript_tab_cache() -> None:
    """Force next _get_applescript_tabs() call to refresh."""
    global _as_tabs_cache_time
    _as_tabs_cache_time = 0.0


def _invalidate_apple_mutation_state(target_id: str) -> None:
    """Discard Apple Events observations after an uncertain mutation."""
    registry.clear()
    _invalidate_applescript_tab_cache()
    coordinator.observations.invalidate_target(
        provider="apple-events",
        mode=OperationMode.SHADOW,
        target_id=target_id,
    )


def _invalidate_shadow_observation(provider: str, target_id: str) -> None:
    coordinator.observations.invalidate_target(
        provider=provider,
        mode=OperationMode.SHADOW,
        target_id=target_id,
    )


def _resolve_applescript_target(target_id: str):
    """Resolve one fresh Apple Events target by browser-owned identity only."""
    if not target_id.startswith("apple-events:"):
        return None, "ERROR: Requested target does not belong to Apple Events."
    tabs = _get_applescript_tabs(force=True)
    target = next(
        (candidate for candidate in tabs if candidate.identifier == target_id),
        None,
    )
    if target is None:
        return None, "ERROR: STALE_TARGET: Apple Events tab is unavailable."
    if not getattr(target, "id", ""):
        return None, (
            "ERROR: UNSUPPORTED_CAPABILITY: browser did not expose a stable tab ID."
        )
    return target, ""


def _normalize_url(url: str) -> str:
    """Normalize URL for comparison: strip trailing slash, lowercase scheme+host."""
    url = url.rstrip("/")
    # Lowercase scheme and host only
    if "://" in url:
        scheme_host, _, path = url.partition("://")
        host, _, rest = path.partition("/")
        url = f"{scheme_host.lower()}://{host.lower()}" + (f"/{rest}" if rest else "")
    return url


def _resolve_applescript_tab(global_index: int) -> tuple[int, int, str]:
    """Translate global tab index to AppleScript (window_index, tab_index).

    Strategy:
    1. Get AppleScript tab list (cached 2s)
    2. If CDP _cached_tabs exists, match by URL to find the correct AS tab
    3. If no CDP cache, use flat AppleScript list order as global index

    Returns: (window_index, per_window_tab_index, error_string)
    """
    as_tabs = _get_applescript_tabs()
    if not as_tabs:
        logger.debug("_resolve_applescript_tab: no AS tabs, falling back to raw index %d", global_index)
        return 0, global_index, ""  # best-effort fallback

    # Strategy A: CDP cache exists — match by URL
    if _cached_tabs and global_index < len(_cached_tabs):
        target_url = _cached_tabs[global_index].url
        target_title = _cached_tabs[global_index].title
        target_url_norm = _normalize_url(target_url)

        logger.debug(
            "_resolve_applescript_tab: matching index=%d "
            "url_length=%d title_length=%d candidate_count=%d",
            global_index,
            len(target_url),
            len(target_title),
            len(as_tabs),
        )

        # Exact URL match
        for at in as_tabs:
            if at.url == target_url:
                logger.debug("_resolve_applescript_tab: exact URL match → win=%d tab=%d", at.window_index, at.index)
                return at.window_index, at.index, ""
        # Normalized URL match (trailing slash, case)
        for at in as_tabs:
            if _normalize_url(at.url) == target_url_norm:
                logger.debug("_resolve_applescript_tab: normalized URL match → win=%d tab=%d", at.window_index, at.index)
                return at.window_index, at.index, ""
        # Title match
        for at in as_tabs:
            if at.title == target_title:
                logger.debug("_resolve_applescript_tab: title match → win=%d tab=%d", at.window_index, at.index)
                return at.window_index, at.index, ""

        logger.debug(
            "_resolve_applescript_tab: no identity match "
            "candidate_count=%d sampled_count=%d",
            len(as_tabs),
            min(len(as_tabs), 5),
        )

    # Strategy B: No CDP cache — AppleScript flat order IS the global index
    if global_index < len(as_tabs):
        at = as_tabs[global_index]
        logger.debug("_resolve_applescript_tab: Strategy B (flat) → win=%d tab=%d", at.window_index, at.index)
        return at.window_index, at.index, ""

    return 0, global_index, f"ERROR: Tab index {global_index} out of range ({len(as_tabs)} tabs)"


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

    if not args.get("shadow", False):
        return await _handle_new_tab(
            {
                "url": url,
                "query": args.get("query", url),
                "reuse_existing": args.get("reuse_existing", True),
                "shadow": False,
            }
        )

    target_id, target_error = _require_shadow_target_id(args, "shadow navigation")
    if target_error:
        return target_error
    budget = _shared_operation_budget(args, 15.0)
    if not args.get("_shadow_coordinator_locked", False):
        internal = _coordinator_args(
            args,
            budget=budget,
            marker="_shadow_coordinator_locked",
        )
        return await coordinator.execute_shadow(
            target_id,
            lambda: _handle_navigate(internal),
            budget=budget,
            operation_manages_deadline=True,
        )
    session, tab, _provider_error = await _get_cdp_session(args, budget=budget)

    if session is not None and tab is not None:
        dispatched = False
        try:
            await budget.wait_for(
                session.enable_domain("Page"),
                operation="shadow navigation setup",
            )

            async def navigate_once():
                nonlocal dispatched
                dispatched = True
                return await session.send("Page.navigate", {"url": url})

            result, _event = await budget.wait_for(
                session.run_until_event(
                    ("Page.loadEventFired", "Page.navigatedWithinDocument"),
                    navigate_once,
                    timeout=budget.remaining(),
                ),
                operation="shadow navigation",
            )
            coordinator.observations.invalidate_target(
                provider="cdp-persistent",
                mode=OperationMode.SHADOW,
                target_id=tab.id,
            )
            if result.get("errorText"):
                return "ERROR: Shadow navigation was rejected by the browser."
            return "Navigation completed in the requested shadow target."
        except Exception as exc:
            logger.debug(
                "Persistent shadow navigation failed (%s)",
                type(exc).__name__,
            )
            if dispatched:
                coordinator.observations.invalidate_target(
                    provider="cdp-persistent",
                    mode=OperationMode.SHADOW,
                    target_id=tab.id,
                )
                return (
                    "ERROR: OUTCOME_UNKNOWN: navigation may have completed; "
                    "refresh list_tabs/web_tree."
                )
            return "ERROR: Persistent shadow navigation provider failed."

    if tab is not None:
        try:
            result = await budget.wait_for(
                cdp_client.navigate(tab, url),
                operation="legacy shadow navigation",
            )
        except Exception as exc:
            logger.debug("Legacy shadow navigation failed (%s)", type(exc).__name__)
            coordinator.observations.invalidate_target(
                provider="cdp-legacy",
                mode=OperationMode.SHADOW,
                target_id=tab.id,
            )
            return (
                "ERROR: OUTCOME_UNKNOWN: navigation may have completed; "
                "refresh list_tabs/web_tree."
            )
        coordinator.observations.invalidate_target(
            provider="cdp-legacy",
            mode=OperationMode.SHADOW,
            target_id=tab.id,
        )
        if "error" in result:
            return "ERROR: Shadow navigation failed."
        return "Navigation completed in the requested shadow target."

    if sys.platform == "darwin" and _as is not None:
        def apple_navigate():
            if not _as.is_available():
                return None, ""
            target, resolve_err = _resolve_applescript_target(target_id)
            if resolve_err:
                return None, resolve_err
            return (
                _as.navigate_tab_outcome(
                    url,
                    tab_index=target.index,
                    window_index=target.window_index,
                    tab_id=target.id,
                    window_id=target.window_id,
                ),
                "",
            )

        try:
            outcome, apple_error = await _run_native_mutation(
                apple_navigate,
                budget=budget,
                operation="Apple Events shadow navigation",
                worker=apple_worker,
            )
        except _NativeMutationOutcomeUnknown:
            _invalidate_apple_mutation_state(target_id)
            return (
                "ERROR: OUTCOME_UNKNOWN: Apple Events navigation may have completed; "
                "refresh list_tabs/web_tree."
            )
        if apple_error:
            return apple_error
        if outcome is None:
            return "ERROR: The Apple Events shadow navigation provider is unavailable."
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return "ERROR: Apple Events navigation was not dispatched."
        _invalidate_apple_mutation_state(target_id)
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return (
                "ERROR: OUTCOME_UNKNOWN: Apple Events navigation may have completed; "
                "refresh list_tabs/web_tree."
            )
        return "Navigation completed in the requested shadow target."

    return "ERROR: The explicitly requested shadow navigation provider is unavailable."


async def _handle_evaluate(args: dict) -> str:
    expression = args.get("expression")
    if not expression:
        return "ERROR: expression is required."
    if not args.get("shadow", False):
        return (
            "ERROR: JavaScript execution is a protocol-only capability. "
            "Set shadow=true only when background browser automation was explicitly requested."
        )

    target_id, target_error = _require_shadow_target_id(
        args,
        "shadow JavaScript evaluation",
    )
    if target_error:
        return target_error
    budget = _shared_operation_budget(args, 5.0)
    if not args.get("_shadow_coordinator_locked", False):
        internal = _coordinator_args(
            args,
            budget=budget,
            marker="_shadow_coordinator_locked",
        )
        return await coordinator.execute_shadow(
            target_id,
            lambda: _handle_evaluate(internal),
            budget=budget,
            operation_manages_deadline=True,
        )
    session, tab, _provider_error = await _get_cdp_session(args, budget=budget)

    if session is not None and tab is not None:
        dispatched = False
        try:
            await budget.wait_for(
                session.enable_domain("Runtime"),
                operation="shadow JavaScript setup",
            )
            dispatched = True
            result = await budget.wait_for(
                session.send(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                ),
                operation="shadow JavaScript evaluation",
            )
            coordinator.observations.invalidate_target(
                provider="cdp-persistent",
                mode=OperationMode.SHADOW,
                target_id=tab.id,
            )
            if result.get("exceptionDetails"):
                return "ERROR: JavaScript completed with a runtime exception."
            value = result.get("result", {}).get("value")
            if value is None:
                return "JavaScript completed (undefined result)."
            return _javascript_result_summary(value)
        except Exception as exc:
            logger.debug(
                "Persistent JavaScript evaluation failed with %s",
                type(exc).__name__,
            )
            if dispatched:
                coordinator.observations.invalidate_target(
                    provider="cdp-persistent",
                    mode=OperationMode.SHADOW,
                    target_id=tab.id,
                )
                return (
                    "ERROR: OUTCOME_UNKNOWN: JavaScript may have executed; "
                    "no fallback was attempted."
                )
            return "ERROR: Persistent shadow JavaScript provider failed."

    if tab is not None:
        try:
            result = await budget.wait_for(
                cdp_client.evaluate(tab, expression),
                operation="legacy shadow JavaScript evaluation",
            )
        except Exception as exc:
            logger.debug("Legacy JavaScript failed (%s)", type(exc).__name__)
            coordinator.observations.invalidate_target(
                provider="cdp-legacy",
                mode=OperationMode.SHADOW,
                target_id=tab.id,
            )
            return (
                "ERROR: OUTCOME_UNKNOWN: JavaScript may have executed; "
                "no fallback was attempted."
            )
        coordinator.observations.invalidate_target(
            provider="cdp-legacy",
            mode=OperationMode.SHADOW,
            target_id=tab.id,
        )
        if "error" in result:
            return "ERROR: JavaScript completed with a runtime exception."
        value = result.get("value")
        if value is None:
            return "JavaScript completed (undefined result)."
        return _javascript_result_summary(value)

    if _as is not None:
        try:
            def apple_evaluate():
                if not _as.is_available():
                    return None, ""
                target, resolve_err = _resolve_applescript_target(target_id)
                if resolve_err:
                    return None, resolve_err
                return (
                    _as.shadow_execute_js_outcome(
                        expression,
                        tab_index=target.index,
                        window_index=target.window_index,
                        tab_id=target.id,
                        window_id=target.window_id,
                    ),
                    "",
                )

            outcome, apple_error = await _run_native_mutation(
                apple_evaluate,
                budget=budget,
                operation="Apple Events JavaScript evaluation",
                worker=apple_worker,
            )
            if apple_error:
                return apple_error
            if outcome is None:
                return "ERROR: The Apple Events JavaScript provider is unavailable."
            if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
                return "ERROR: Apple Events JavaScript was not dispatched."
            _invalidate_apple_mutation_state(target_id)
            if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
                return (
                    "ERROR: OUTCOME_UNKNOWN: Apple Events JavaScript may have "
                    "executed; no fallback was attempted."
                )
            if outcome.value is None:
                return "JavaScript completed (undefined Apple Events result)."
            return _javascript_result_summary(outcome.value)
        except _NativeMutationOutcomeUnknown:
            _invalidate_apple_mutation_state(target_id)
            return (
                "ERROR: OUTCOME_UNKNOWN: Apple Events JavaScript may have executed; "
                "no fallback was attempted."
            )
        except (OperationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.debug("Apple Events JavaScript failed (%s)", type(exc).__name__)
            return "ERROR: Apple Events JavaScript execution failed."

    return "ERROR: The explicitly requested shadow JavaScript provider is unavailable."


def _javascript_result_summary(value: object) -> str:
    """Describe a JavaScript result without reflecting page/user secrets."""
    size = bounded_json_utf8_size(value)
    result_type = type(value).__name__
    if size.bytes is None:
        size_text = "size unavailable"
    elif size.exact:
        size_text = f"{size.bytes} bytes"
    else:
        size_text = f">{size.bytes - 1} bytes"
    return f"JavaScript completed (result type {result_type}, {size_text})."


def _require_shadow_target_id(args: dict, operation: str) -> tuple[str, str]:
    """Require a stable target before any direct shadow mutation."""
    target_id = str(args.get("target_id", "")).strip()
    if not target_id:
        return "", (
            f"ERROR: {operation} requires target_id from a fresh "
            "list_tabs(shadow=true) result."
        )
    return target_id, ""


def _shared_operation_budget(args: dict, seconds: float) -> OperationBudget:
    """Reuse one end-to-end deadline across coordinator recursion."""
    existing = args.get("_operation_budget")
    if isinstance(existing, OperationBudget):
        return existing
    return OperationBudget.start(seconds)


def _coordinator_args(
    args: dict,
    *,
    budget: OperationBudget,
    marker: str,
) -> dict:
    internal = dict(args)
    internal["_operation_budget"] = budget
    internal[marker] = True
    return internal


async def _handle_press_key(args: dict) -> str:
    budget = _shared_operation_budget(args, 5.0)
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

    if not args.get("shadow", False):
        if not args.get("_foreground_coordinator_locked", False):
            internal = _coordinator_args(
                args,
                budget=budget,
                marker="_foreground_coordinator_locked",
            )
            return await coordinator.execute_foreground(
                lambda: _handle_press_key(internal),
                budget=budget,
                operation_manages_deadline=True,
            )
        input_backend = _input_backend
        if input_backend is None or not await input_worker.run(
            input_backend.is_available,
            budget=budget,
            operation="key input capability check",
        ):
            return "ERROR: No foreground input backend is available."
        if pid is not None:
            focus_ok, focus_err = await _verify_focus(pid, budget=budget)
            if not focus_ok:
                return f"ERROR: {focus_err}. Key '{key}' was not sent."
        try:
            if modifiers:
                native_mods = [mod_map.get(m.lower(), m.lower()) for m in modifiers]
                success = await _run_native_mutation(
                    lambda: input_backend.hotkey(*(native_mods + [native_key])),
                    budget=budget,
                    operation="foreground hotkey",
                    worker=input_worker,
                )
            else:
                success = await _run_native_mutation(
                    lambda: input_backend.press_key(native_key),
                    budget=budget,
                    operation="foreground key press",
                    worker=input_worker,
                )
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: foreground key input may have been applied; "
                "refresh the target before retrying."
            )
        if success:
            return f"pressed {mod_str}{key}"
        return f"ERROR: Could not press key '{key}' through foreground OS input."

    target_id, target_error = _require_shadow_target_id(args, "shadow key input")
    if target_error:
        return target_error

    async def press_shadow_key() -> str:
        session, tab, provider_error = await _get_cdp_session(args, budget=budget)
        if tab is None:
            return provider_error or "ERROR: Requested shadow target is unavailable."
        provider = "cdp-persistent" if session is not None else "cdp-legacy"
        try:
            if session is not None:
                await budget.wait_for(
                    session.press_key(key, modifiers),
                    operation="shadow key input",
                )
                success = True
            else:
                success = await budget.wait_for(
                    cdp_client.press_key(tab, key, modifiers),
                    operation="legacy shadow key input",
                )
        except Exception as exc:
            logger.debug("Shadow key input failed (%s)", type(exc).__name__)
            _invalidate_shadow_observation(provider, target_id)
            return (
                "ERROR: OUTCOME_UNKNOWN: key input may have been applied; "
                "no fallback was attempted."
            )
        _invalidate_shadow_observation(provider, target_id)
        if success:
            return f"pressed {mod_str}{key}"
        return "ERROR: OUTCOME_UNKNOWN: key input may have been applied."

    return await coordinator.execute_shadow(
        target_id,
        press_shadow_key,
        budget=budget,
        operation_manages_deadline=True,
    )


_MAX_WAIT_TIMEOUT = 60.0  # seconds — cap to prevent MCP server DoS


async def _handle_wait_for(args: dict) -> str:
    role = args.get("role", "")
    name = args.get("name", "")
    timeout = max(0.0, min(float(args.get("timeout", 5.0)), _MAX_WAIT_TIMEOUT))
    budget = OperationBudget.start(timeout)

    if not role and not name:
        return "ERROR: Specify at least one of: role, name"

    pid = args.get("pid")
    if pid is not None and native_adapter:
        result = await wait_for_native_element(
            native_adapter,
            int(pid),
            role=role,
            name=name,
            timeout=timeout,
            budget=budget,
            worker=native_worker,
        )
        if result.element is not None:
            registry.register_element(result.element)
            snapshot = coordinator.observations.create(
                provider="native",
                mode=OperationMode.FOREGROUND,
                target_id=f"pid:{int(pid)}",
                generation=0,
                revision=time.monotonic_ns(),
                elements=[
                    ElementRecord(
                        local_id=result.element.id,
                        value=result.element,
                    )
                ],
            )
            mode = "native events" if result.event_driven else "adaptive native fallback"
            return (
                f"snapshot={snapshot.token}\n"
                f"Found [{result.element.id}] {result.element.role} "
                f"\"{result.element.name}\" after {result.elapsed:.2f}s ({mode})"
            )
        return f"Timeout after {timeout}s: no element matching role='{role}' name='{name}' found."

    if pid is not None and not args.get("shadow", False):
        return "ERROR: The foreground native accessibility provider is unavailable."

    if not args.get("shadow", False):
        return (
            "Foreground wait requires pid. Call list_tabs with a query, then use the "
            "returned browser PID; set shadow=true only for explicit background CDP waiting."
        )

    target_id, target_error = _require_shadow_target_id(args, "shadow wait")
    if target_error:
        return target_error
    session, tab, provider_error = await _get_cdp_session(args, budget=budget)
    if session is None or tab is None:
        return provider_error or "ERROR: Event-driven shadow wait provider is unavailable."

    raw_node = None
    revision = 0
    for attempt in range(2):
        revision_before = await budget.wait_for(
            _persistent_document_revision(session),
            operation="shadow wait document revision",
        )

        async def wait_once():
            return await session.wait_for_ax_node(
                role=role,
                name=name,
                timeout=timeout,
            )

        raw_node = await coordinator.observe(
            (
                "shadow-wait",
                target_id,
                session.generation,
                revision_before,
                role,
                name,
                timeout,
            ),
            wait_once,
            budget=budget,
        )
        if raw_node is None:
            break
        revision = await budget.wait_for(
            _persistent_document_revision(session),
            operation="shadow wait document revision",
        )
        if revision == revision_before:
            break
        raw_node = None
        if attempt == 1:
            return "ERROR: Shadow document changed during wait; retry."
    if raw_node is None:
        return f"Timeout: element not found after {timeout}s."

    element = cdp_client._node_to_element(raw_node)
    if element is None:
        return "ERROR: Matching accessibility node could not be converted."
    registry.register_element(element)
    backend_id = element.platform_ref
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id=target_id,
        generation=session.generation,
        revision=revision,
        elements=[
            ElementRecord(
                local_id=element.id,
                value=element,
                actionable=(
                    isinstance(backend_id, int)
                    and not isinstance(backend_id, bool)
                    and backend_id > 0
                ),
            )
        ],
    )
    return (
        f"snapshot={snapshot.token}\n"
        f"Found element: [{element.id}] {element.role} \"{element.name}\" "
        "(event-driven shadow wait)\nUse this [id] with click or type."
    )


async def _handle_new_tab(args: dict) -> str:
    global _cached_tabs

    url = str(args.get("url", "about:blank"))
    url_error = _validate_url(url)
    if url_error:
        return url_error
    shadow = bool(args.get("shadow", False))
    budget = _shared_operation_budget(args, 10.0)
    marker = (
        "_shadow_coordinator_locked"
        if shadow
        else "_foreground_coordinator_locked"
    )
    if not args.get(marker, False):
        internal = _coordinator_args(args, budget=budget, marker=marker)
        if shadow:
            return await coordinator.execute_shadow(
                ("shadow-provider", "new-tab"),
                lambda: _handle_new_tab(internal),
                budget=budget,
                operation_manages_deadline=True,
            )
        return await coordinator.execute_foreground(
            lambda: _handle_new_tab(internal),
            budget=budget,
            operation_manages_deadline=True,
        )
    query = str(args.get("query", "")).strip()
    if not query and url != "about:blank":
        query = url

    if not shadow and args.get("reuse_existing", True) and query:
        native_targets = await _collect_native_browser_targets(budget=budget)
        existing = best_browser_target(native_targets, query)
        if existing is not None:
            try:
                activated = await _activate_browser_target_and_wait(
                    existing,
                    budget=budget,
                )
            except _NativeMutationOutcomeUnknown:
                return (
                    "ERROR: OUTCOME_UNKNOWN: the matching browser target may have "
                    "been activated; refresh list_tabs before retrying."
                )
            if activated:
                return (
                    f"Reused existing {existing.browser} tab in PID {existing.pid}: "
                    f"{existing.title}"
                )
            return (
                "ERROR: A suitable already-open match was found but could not be activated; "
                "no duplicate tab was opened. Call list_tabs with the same query and use its PID."
            )

    if shadow:
        available = await budget.wait_for(
            cdp_client.is_available(),
            operation="shadow new-tab capability check",
        )
        if not available:
            return "ERROR: The optional shadow tab provider is not connected."
        if not _cached_tabs:
            tabs = await budget.wait_for(
                cdp_client.list_tabs(),
                operation="shadow tab inventory",
            )
            async with _tabs_lock:
                _cached_tabs.extend(tabs)
        try:
            tab = await budget.wait_for(
                cdp_client.new_tab(url),
                operation="shadow new-tab creation",
            )
        except Exception as exc:
            logger.debug("Shadow new-tab creation failed (%s)", type(exc).__name__)
            async with _tabs_lock:
                _cached_tabs = []
            return (
                "ERROR: OUTCOME_UNKNOWN: a shadow tab may have been created; "
                "refresh list_tabs before retrying."
            )
        if tab is None:
            return "ERROR: Could not create new tab."
        async with _tabs_lock:
            _cached_tabs.append(tab)
            idx = len(_cached_tabs) - 1
        return f"New shadow tab [{idx}] target_id={str(tab.id)[:512]}."

    try:
        success, _msg = await _run_native_mutation(
            lambda: _pu.open_url_in_browser(url),
            budget=budget,
            operation="default-browser new tab",
            worker=system_worker,
        )
    except _NativeMutationOutcomeUnknown:
        _invalidate_native_mutation_state()
        return (
            "ERROR: OUTCOME_UNKNOWN: a foreground browser tab may have opened; "
            "refresh list_tabs before retrying."
        )
    if success:
        return "Opened the requested URL in the system default browser."
    return "ERROR: The operating system could not open a foreground browser tab."


async def _handle_close_tab(args: dict) -> str:
    global _cached_tabs
    budget = _shared_operation_budget(args, 5.0)

    if not args.get("shadow", False):
        if args.get("tab_index") is not None:
            return (
                "ERROR: Foreground close does not accept positional tab_index; "
                "use target_id from a fresh list_tabs result or a unique title."
            )
        if not args.get("_foreground_coordinator_locked", False):
            internal = _coordinator_args(
                args,
                budget=budget,
                marker="_foreground_coordinator_locked",
            )
            return await coordinator.execute_foreground(
                lambda: _handle_close_tab(internal),
                budget=budget,
                operation_manages_deadline=True,
            )
        targets = await _collect_native_browser_targets(budget=budget)
        target_id = str(args.get("target_id", "")).strip()
        title_query = str(args.get("title", "")).strip()
        if target_id:
            target = next(
                (
                    candidate
                    for candidate in targets
                    if candidate.identifier == target_id
                ),
                None,
            )
            if target is None:
                return "ERROR: Native target is stale. Call list_tabs again."
        elif title_query:
            query = title_query.casefold()
            matches = [target for target in targets if query in target.title.casefold()]
            if not matches:
                return "ERROR: No foreground browser tab matched. Call list_tabs first."
            if len(matches) > 1:
                return (
                    f"ERROR: {len(matches)} foreground tabs matched; use target_id "
                    "from list_tabs."
                )
            target = matches[0]
        else:
            return "ERROR: Provide target_id or a unique title from a fresh list_tabs result."

        if target.source == "native-window":
            return (
                "ERROR: UNSUPPORTED_CAPABILITY: this browser exposes only a window, "
                "not an exact tab control; the close shortcut was not sent."
            )
        if target.element is None or target.window_element is None:
            return (
                "ERROR: TARGET_MISMATCH: the exact tab and owning window could not "
                "be verified; call list_tabs again."
            )
        try:
            valid = await native_worker.run(
                lambda: (
                    native_adapter.is_element_valid(target.element)
                    and native_adapter.is_element_valid(target.window_element)
                ),
                budget=budget,
                operation="native browser target validity check",
            )
            if not valid:
                return "ERROR: Native target is stale. Call list_tabs again."
        except Exception:
            return "ERROR: Native target is stale. Call list_tabs again."

        try:
            activated = await _activate_browser_target_and_wait(
                target,
                budget=budget,
            )
        except _NativeMutationOutcomeUnknown:
            return (
                "ERROR: OUTCOME_UNKNOWN: the browser target may have been activated; "
                "the tab close shortcut was not sent. Refresh list_tabs before retrying."
            )
        if not activated:
            return (
                f"ERROR: Found a {target.browser} tab, but could not verify "
                "that it was selected and frontmost; "
                "the tab was not closed."
            )
        if _input_backend is None or not await input_worker.run(
            _input_backend.is_available,
            budget=budget,
            operation="close-tab input capability check",
        ):
            return "ERROR: Foreground input is unavailable; the tab was not closed."
        exactly_active = await native_worker.run(
            lambda: _browser_target_is_exactly_active(target),
            budget=budget,
            operation="pre-close exact browser target verification",
        )
        if not exactly_active:
            return (
                "ERROR: FOCUS_MISMATCH: the exact browser tab or owning window "
                "changed before close; the shortcut was not sent."
            )
        modifier = "command" if sys.platform == "darwin" else "control"
        try:
            closed = await _run_native_mutation(
                lambda: _input_backend.hotkey(modifier, "w"),
                budget=budget,
                operation="foreground close-tab shortcut",
                worker=input_worker,
            )
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=target.pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: the foreground browser tab may have closed; "
                "refresh list_tabs before retrying."
            )
        if closed:
            _invalidate_native_mutation_state(pid=target.pid)
            return f"Closed foreground {target.browser} tab."
        return f"ERROR: Could not close foreground {target.browser} tab."

    target_id, target_error = _require_shadow_target_id(args, "shadow close")
    if target_error:
        return target_error

    async def close_shadow_target() -> str:
        global _cached_tabs
        session, tab, provider_error = await _get_cdp_session(args, budget=budget)
        if tab is None:
            return provider_error or "ERROR: Requested shadow target is unavailable."
        provider = "cdp-persistent" if session is not None else "cdp-legacy"
        try:
            if session is not None:
                success = await budget.wait_for(
                    cdp_pool.close_target(target_id),
                    operation="shadow target close",
                )
            else:
                success = await budget.wait_for(
                    cdp_client.close_tab(tab),
                    operation="legacy shadow target close",
                )
        except Exception as exc:
            logger.debug("Shadow target close failed (%s)", type(exc).__name__)
            _invalidate_shadow_observation(provider, target_id)
            async with _tabs_lock:
                _cached_tabs = []
            return "ERROR: OUTCOME_UNKNOWN: the shadow target may have closed."
        if not success:
            _invalidate_shadow_observation(provider, target_id)
            async with _tabs_lock:
                _cached_tabs = []
            return "ERROR: OUTCOME_UNKNOWN: the shadow target may have closed."
        _invalidate_shadow_observation(provider, target_id)
        async with _tabs_lock:
            _cached_tabs = [
                candidate for candidate in _cached_tabs if candidate.id != target_id
            ]
        _invalidate_applescript_tab_cache()
        return "Closed the requested shadow target."

    return await coordinator.execute_shadow(
        target_id,
        close_shadow_target,
        budget=budget,
        operation_manages_deadline=True,
    )


async def _handle_dialog(args: dict) -> str:
    if not args.get("shadow", False):
        return (
            "ERROR: Protocol-level JavaScript dialog handling requires explicit shadow=true. "
            "For a visible native dialog, inspect it with tree and interact in the foreground."
        )
    target_id, target_error = _require_shadow_target_id(args, "shadow dialog")
    if target_error:
        return target_error
    budget = OperationBudget.start(5.0)
    accept = bool(args.get("accept", True))
    prompt_text = str(args.get("prompt_text", ""))

    async def handle_shadow_dialog() -> str:
        session, tab, provider_error = await _get_cdp_session(args, budget=budget)
        if tab is None:
            return provider_error or "ERROR: Requested shadow target is unavailable."
        provider = "cdp-persistent" if session is not None else "cdp-legacy"
        try:
            if session is not None:
                await budget.wait_for(
                    session.handle_dialog(accept, prompt_text),
                    operation="shadow dialog handling",
                )
                success = True
            else:
                success = await budget.wait_for(
                    cdp_client.handle_dialog(tab, accept, prompt_text),
                    operation="legacy shadow dialog handling",
                )
        except Exception as exc:
            logger.debug("Shadow dialog handling failed (%s)", type(exc).__name__)
            _invalidate_shadow_observation(provider, target_id)
            return "ERROR: OUTCOME_UNKNOWN: the dialog may have been handled."
        _invalidate_shadow_observation(provider, target_id)
        if success:
            return "Accepted dialog." if accept else "Dismissed dialog."
        return "ERROR: No dialog was handled or the outcome could not be verified."

    return await coordinator.execute_shadow(
        target_id,
        handle_shadow_dialog,
        budget=budget,
        operation_manages_deadline=True,
    )


def _upload_security_path_key(path) -> str:
    """Return a stable comparison key for an already-canonical filesystem path."""
    import os
    import unicodedata

    normalized = os.path.normcase(os.path.normpath(os.fspath(path)))
    return unicodedata.normalize("NFC", normalized).casefold()


def _path_matches_protected_upload_root(candidate, root) -> bool:
    """Fail closed when a canonical file is inside or aliases a protected root."""
    import os
    import pathlib

    try:
        resolved_root = pathlib.Path(root).resolve(strict=False)
        candidate_key = _upload_security_path_key(candidate)
        root_key = _upload_security_path_key(resolved_root)
        if pathlib.Path(candidate_key).is_relative_to(pathlib.Path(root_key)):
            return True
        resolved_root.stat()
    except FileNotFoundError:
        # A missing protected root cannot alias an existing candidate. The
        # case-folded lexical check above still protects case aliases.
        return False
    except (OSError, RuntimeError, ValueError):
        return True

    for ancestor in (candidate, *candidate.parents):
        try:
            if os.path.samefile(ancestor, resolved_root):
                return True
        except (OSError, ValueError):
            return True
    return False


def _validate_upload_paths(files, *, home=None) -> tuple[list[str], str]:
    """Resolve regular upload files and reject protected filesystem aliases."""
    import os
    import pathlib
    import stat

    resolved_home = pathlib.Path.home() if home is None else pathlib.Path(home)
    blocked_roots = [
        resolved_home / ".ssh",
        resolved_home / ".aws",
        resolved_home / ".gnupg",
        resolved_home / ".config" / "gcloud",
        resolved_home / ".config" / "op",
        resolved_home / ".kube",
        resolved_home / ".docker",
        resolved_home / ".netrc",
        resolved_home / ".npmrc",
        resolved_home / ".pypirc",
        resolved_home / ".gem" / "credentials",
        pathlib.Path("/etc"),
    ]
    validated_paths: list[str] = []
    for path in files:
        try:
            candidate = pathlib.Path(os.path.abspath(os.fspath(path))).resolve(
                strict=True
            )
            if not stat.S_ISREG(candidate.stat().st_mode):
                return [], "missing"
        except (OSError, RuntimeError, TypeError, ValueError):
            return [], "missing"
        if any(
            _path_matches_protected_upload_root(candidate, root)
            for root in blocked_roots
        ):
            return [], "protected"
        validated_paths.append(str(candidate))
    return validated_paths, ""


async def _handle_file_upload(args: dict) -> str:

    if not args.get("shadow", False):
        return (
            "ERROR: Direct DOM file injection requires explicit shadow=true. "
            "For foreground upload, click the file control and automate the native picker."
        )

    element_id = args.get("id")
    files = args.get("files", [])
    if element_id is None:
        return "ERROR: id is required."
    if not files:
        return "ERROR: files list is required."

    element, snapshot, mode, action_key, resolve_error = _resolve_action_element(
        args,
        "upload",
    )
    if resolve_error:
        return resolve_error
    if mode is not OperationMode.SHADOW or snapshot is None or element is None:
        return "ERROR: upload requires an explicit shadow snapshot."
    if element.source not in {"cdp", "shadow-dom"} or element.platform_ref is None:
        return "ERROR: The selected element is not actionable by the shadow file provider."

    budget = OperationBudget.start(5.0)

    def validate_paths() -> tuple[list[str], str]:
        return _validate_upload_paths(files)

    validated, validation_error = await system_worker.run(
        validate_paths,
        budget=budget,
        operation="upload path validation",
    )
    if validation_error == "missing":
        return "ERROR: One requested upload file does not exist or is not a regular file."
    if validation_error:
        return "ERROR: One requested upload file is in a protected location."

    async def upload_to_snapshot_target() -> str:
        if snapshot.provider == "cdp-persistent":
            try:
                session = await _current_persistent_snapshot_session(
                    snapshot,
                    budget=budget,
                )
                await _assert_persistent_snapshot_current(
                    session,
                    snapshot,
                    budget=budget,
                )
                await budget.wait_for(
                    session.set_file_input(element.platform_ref, validated),
                    operation="shadow file input",
                )
            except Exception as exc:
                logger.debug("Shadow upload failed (%s)", type(exc).__name__)
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return "ERROR: OUTCOME_UNKNOWN: files may have been attached."
        elif snapshot.provider == "cdp-legacy":
            err = await _ensure_tabs(force=True, budget=budget)
            if err:
                return err
            tab = next(
                (candidate for candidate in _cached_tabs if candidate.id == snapshot.target_id),
                None,
            )
            if tab is None:
                return "ERROR: STALE_SNAPSHOT: shadow target closed."
            try:
                success = await budget.wait_for(
                    cdp_client.set_file_input(
                        tab,
                        element.platform_ref,
                        validated,
                        expected_revision=snapshot.revision,
                    ),
                    operation="legacy shadow file input",
                )
            except CDPDocumentChangedError:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return "ERROR: STALE_SNAPSHOT: page changed before file input."
            except Exception as exc:
                logger.debug("Legacy shadow upload failed (%s)", type(exc).__name__)
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return "ERROR: OUTCOME_UNKNOWN: files may have been attached."
            if not success:
                _invalidate_shadow_observation(
                    snapshot.provider,
                    snapshot.target_id,
                )
                return "ERROR: OUTCOME_UNKNOWN: files may have been attached."
        else:
            return "ERROR: Unsupported upload snapshot provider."
        _invalidate_shadow_observation(
            snapshot.provider,
            snapshot.target_id,
        )
        return f"Uploaded {len(validated)} file(s) to [{element_id}]."

    return await coordinator.execute_shadow(
        action_key,
        upload_to_snapshot_target,
        budget=budget,
        operation_manages_deadline=True,
    )


async def _handle_scroll(args: dict) -> str:
    x = args.get("x", 400)
    y = args.get("y", 400)
    delta_x = args.get("delta_x", 0)
    delta_y = args.get("delta_y", 300)
    pid = args.get("pid")
    budget = _shared_operation_budget(args, 5.0)

    # Foreground is the default for both browsers and desktop applications.
    if not args.get("shadow", False):
        if not args.get("_foreground_coordinator_locked", False):
            internal = _coordinator_args(
                args,
                budget=budget,
                marker="_foreground_coordinator_locked",
            )
            return await coordinator.execute_foreground(
                lambda: _handle_scroll(internal),
                budget=budget,
                operation_manages_deadline=True,
            )
        input_backend = _input_backend
        if input_backend is not None and await input_worker.run(
            input_backend.is_available,
            budget=budget,
            operation="scroll capability check",
        ):
            if pid is not None:
                focus_ok, focus_err = await _verify_focus(pid, budget=budget)
                if not focus_ok:
                    return f"ERROR: {focus_err}. Scroll was not sent."
            # The public API is pixel-like; native backends consume bounded
            # wheel steps/buttons.  Normalize once at the boundary so a
            # default 300px request never becomes 300 low-level events.
            def native_steps(delta: int) -> int:
                if not delta:
                    return 0
                return max(-20, min(20, int(round(delta / 100)))) or (1 if delta > 0 else -1)

            native_delta_y = -native_steps(delta_y)
            native_delta_x = -native_steps(delta_x)
            try:
                success = await _run_native_mutation(
                    lambda: input_backend.scroll(
                        x,
                        y,
                        delta_x=native_delta_x,
                        delta_y=native_delta_y,
                    ),
                    budget=budget,
                    operation="foreground scroll",
                    worker=input_worker,
                )
            except _NativeMutationOutcomeUnknown:
                _invalidate_native_mutation_state(pid=pid)
                return (
                    "ERROR: OUTCOME_UNKNOWN: foreground scrolling may have occurred; "
                    "refresh the target before retrying."
                )
            if success:
                direction = "down" if delta_y > 0 else "up" if delta_y < 0 else ""
                if delta_x:
                    direction += (" + right" if delta_x > 0 else " + left")
                return f"scrolled {direction}"
        return "ERROR: Foreground scroll failed or no input backend is available."

    target_id, target_error = _require_shadow_target_id(args, "shadow scroll")
    if target_error:
        return target_error

    async def scroll_shadow_target() -> str:
        session, tab, provider_error = await _get_cdp_session(args, budget=budget)
        if tab is None:
            return provider_error or "ERROR: Requested shadow target is unavailable."
        provider = "cdp-persistent" if session is not None else "cdp-legacy"
        try:
            if session is not None:
                await budget.wait_for(
                    session.scroll(x, y, delta_x, delta_y),
                    operation="shadow scroll",
                )
                success = True
            else:
                success = await budget.wait_for(
                    cdp_client.scroll(tab, x, y, delta_x, delta_y),
                    operation="legacy shadow scroll",
                )
        except Exception as exc:
            logger.debug("Shadow scroll failed (%s)", type(exc).__name__)
            _invalidate_shadow_observation(provider, target_id)
            return "ERROR: OUTCOME_UNKNOWN: scrolling may have occurred."
        _invalidate_shadow_observation(provider, target_id)
        if not success:
            return "ERROR: OUTCOME_UNKNOWN: scrolling may have occurred."
        return "Scrolled the requested shadow target."

    return await coordinator.execute_shadow(
        target_id,
        scroll_shadow_target,
        budget=budget,
        operation_manages_deadline=True,
    )


async def _handle_drag(args: dict) -> str:
    for field in ("from_x", "from_y", "to_x", "to_y"):
        if args.get(field) is None:
            return f"ERROR: {field} is required."

    from_x = args["from_x"]
    from_y = args["from_y"]
    to_x = args["to_x"]
    to_y = args["to_y"]
    budget = _shared_operation_budget(args, 5.0)

    if not args.get("shadow", False):
        if not args.get("_foreground_coordinator_locked", False):
            internal = _coordinator_args(
                args,
                budget=budget,
                marker="_foreground_coordinator_locked",
            )
            return await coordinator.execute_foreground(
                lambda: _handle_drag(internal),
                budget=budget,
                operation_manages_deadline=True,
            )
        input_backend = _input_backend
        pid = args.get("pid")
        available = bool(
            input_backend is not None
            and await input_worker.run(
                input_backend.is_available,
                budget=budget,
                operation="drag capability check",
            )
        )
        if pid is not None and available:
            focus_ok, focus_err = await _verify_focus(pid, budget=budget)
            if not focus_ok:
                return f"ERROR: {focus_err}. Drag was not sent."
        if available and hasattr(input_backend, "drag"):
            try:
                success = await _run_native_mutation(
                    lambda: input_backend.drag(from_x, from_y, to_x, to_y),
                    budget=budget,
                    operation="foreground drag",
                    worker=input_worker,
                )
            except _NativeMutationOutcomeUnknown:
                _invalidate_native_mutation_state(pid=pid)
                return (
                    "ERROR: OUTCOME_UNKNOWN: foreground dragging may have occurred; "
                    "refresh the target before retrying."
                )
            if success:
                return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        return "ERROR: Foreground drag failed or no input backend is available."

    target_id, target_error = _require_shadow_target_id(args, "shadow drag")
    if target_error:
        return target_error

    async def drag_shadow_target() -> str:
        session, tab, provider_error = await _get_cdp_session(args, budget=budget)
        if tab is None:
            return provider_error or "ERROR: Requested shadow target is unavailable."
        provider = "cdp-persistent" if session is not None else "cdp-legacy"
        try:
            if session is not None:
                await budget.wait_for(
                    session.drag(from_x, from_y, to_x, to_y),
                    operation="shadow drag",
                )
                success = True
            else:
                success = await budget.wait_for(
                    cdp_client.drag(tab, from_x, from_y, to_x, to_y),
                    operation="legacy shadow drag",
                )
        except Exception as exc:
            logger.debug("Shadow drag failed (%s)", type(exc).__name__)
            _invalidate_shadow_observation(provider, target_id)
            return "ERROR: OUTCOME_UNKNOWN: dragging may have occurred."
        _invalidate_shadow_observation(provider, target_id)
        if not success:
            return "ERROR: OUTCOME_UNKNOWN: dragging may have occurred."
        return "Dragged in the requested shadow target."

    return await coordinator.execute_shadow(
        target_id,
        drag_shadow_target,
        budget=budget,
        operation_manages_deadline=True,
    )


async def _handle_fill_form(args: dict) -> str:
    fields = args.get("fields", [])
    if not fields:
        return "ERROR: fields list is required."
    snapshot_token = str(args.get("snapshot", "")).strip()
    if not snapshot_token:
        return "ERROR: fill_form requires snapshot from tree/web_tree."

    shadow = bool(args.get("shadow", False))
    budget = OperationBudget.start(15.0)
    prepared: list[tuple[int, str, UIElement, ObservationSnapshot]] = []
    action_key = ""
    mode = OperationMode.SHADOW if shadow else OperationMode.FOREGROUND
    failures = 0
    for field in fields:
        element_id = field.get("id")
        element, metadata, resolved_mode, key, error = _resolve_action_element(
            {
                "id": element_id,
                "snapshot": snapshot_token,
                "shadow": shadow,
            },
            "fill_form",
        )
        if error or element is None or metadata is None or resolved_mode is not mode:
            failures += 1
            continue
        if action_key and key != action_key:
            failures += 1
            continue
        action_key = key
        prepared.append(
            (
                element_id,
                str(field.get("value", "")),
                element,
                metadata,
            )
        )
    if not prepared:
        return f"ERROR: No fields were resolvable in the supplied snapshot ({failures} failed)."

    async def fill_snapshot_fields() -> str:
        filled = 0
        local_failures = failures
        uncertain = False
        for index, (element_id, value, element, metadata) in enumerate(prepared):
            if budget.expired:
                local_failures += len(prepared) - index
                break
            try:
                result = await _handle_type_resolved(
                    element_id,
                    value,
                    element,
                    snapshot=metadata,
                    budget=budget,
                )
            except OperationError:
                local_failures += 1
                break
            if result.startswith("typed "):
                filled += 1
                continue
            local_failures += 1
            if "OUTCOME_UNKNOWN" in result:
                uncertain = True
                break
        summary = f"filled {filled} field(s); {local_failures} failed"
        if uncertain:
            return f"ERROR: OUTCOME_UNKNOWN: {summary}; stopped after an uncertain outcome"
        if local_failures:
            return f"ERROR: PARTIAL_FAILURE: {summary}"
        return summary

    if mode is OperationMode.SHADOW:
        return await coordinator.execute_shadow(
            action_key,
            fill_snapshot_fields,
            budget=budget,
            operation_manages_deadline=True,
        )
    return await coordinator.execute_foreground(
        fill_snapshot_fields,
        budget=budget,
        operation_manages_deadline=True,
    )


# ── New tool handlers ────────────────────────────────────────────────

async def _handle_hover(args: dict) -> str:
    hover_x = args.get("x")
    hover_y = args.get("y")
    element_id = args.get("id")
    budget = _shared_operation_budget(args, 5.0)
    if not args.get("_foreground_coordinator_locked", False):
        internal = _coordinator_args(
            args,
            budget=budget,
            marker="_foreground_coordinator_locked",
        )
        return await coordinator.execute_foreground(
            lambda: _handle_hover(internal),
            budget=budget,
            operation_manages_deadline=True,
        )
    input_backend = _input_backend

    available = bool(
        input_backend is not None
        and await input_worker.run(
            input_backend.is_available,
            budget=budget,
            operation="hover input capability check",
        )
    )

    if hover_x is not None and hover_y is not None:
        if available:
            try:
                moved = await _run_native_mutation(
                    lambda: input_backend.move_mouse(hover_x, hover_y),
                    budget=budget,
                    operation="coordinate hover",
                    worker=input_worker,
                )
            except _NativeMutationOutcomeUnknown:
                _invalidate_native_mutation_state(pid=args.get("pid"))
                return (
                    "ERROR: OUTCOME_UNKNOWN: foreground pointer movement may have "
                    "occurred; refresh the target before retrying."
                )
            if moved:
                return f"hovered at ({hover_x}, {hover_y})"
        return "ERROR: No input backend available."

    if element_id is None:
        return "ERROR: id or (x, y) is required."

    snapshot_token = str(args.get("snapshot", "")).strip()
    if not snapshot_token:
        return "ERROR: hover by id requires snapshot from tree."
    element, snapshot, mode, _action_key, error = _resolve_action_element(
        {"id": element_id, "snapshot": snapshot_token, "shadow": False},
        "hover",
    )
    if error:
        return error
    if mode is not OperationMode.FOREGROUND or element is None:
        return "ERROR: Hover by ID requires a foreground snapshot."

    if hasattr(native_adapter, "is_element_valid") and not await native_worker.run(
        lambda: native_adapter.is_element_valid(element),
        budget=budget,
        operation="native hover element validity check",
    ):
        return f"ERROR: hover [{element_id}]: element stale (UI changed); refresh tree."

    if element.bounds:
        x, y, w, h = element.bounds
        cx, cy = x + w // 2, y + h // 2
        if available:
            if element.pid:
                focus_ok, focus_err = await _verify_focus(element.pid, budget=budget)
                if not focus_ok:
                    return f"ERROR: {focus_err}. Hover was not sent."
            try:
                moved = await _run_native_mutation(
                    lambda: input_backend.move_mouse(cx, cy),
                    budget=budget,
                    operation="element hover",
                    worker=input_worker,
                )
            except _NativeMutationOutcomeUnknown:
                _invalidate_native_mutation_state(
                    snapshot=snapshot,
                    pid=element.pid,
                )
                return (
                    "ERROR: OUTCOME_UNKNOWN: foreground pointer movement may have "
                    "occurred; refresh the target before retrying."
                )
            if moved:
                return f"hovered [{element_id}]"

    return f"ERROR: Element [{element_id}] has no bounds for hover."


def _handle_app(args: dict) -> str:
    action = args.get("action", "").lower()
    name = args.get("name", "")
    if not action or not name:
        return "ERROR: action and name are required."

    if sys.platform != "darwin":
        return "ERROR: app currently only supports macOS."

    try:
        from AppKit import NSWorkspace
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

    elif action in {"focus", "quit"}:
        query = name.casefold().strip()
        running = list(ws.runningApplications())
        exact = [
            app
            for app in running
            if query
            in {
                (app.localizedName() or "").casefold(),
                (app.bundleIdentifier() or "").casefold(),
            }
        ]
        matches = exact or [
            app
            for app in running
            if query and query in (app.localizedName() or "").casefold()
        ]
        if not matches:
            return f"ERROR: App '{name}' not found running."
        if len(matches) > 1:
            return (
                f"ERROR: AMBIGUOUS_TARGET: {len(matches)} running apps matched "
                f"'{name}'; use an exact app name or bundle ID."
            )
        app = matches[0]
        app_name = app.localizedName() or ""
        if action == "focus":
            if app.activateWithOptions_(0):
                return f"Focused '{app_name}' (PID {app.processIdentifier()})."
            return f"ERROR: App '{app_name}' rejected the focus request."
        if app.terminate():
            return f"Quit '{app_name}'."
        return f"ERROR: App '{app_name}' rejected the quit request."

    return f"ERROR: Unknown action '{action}'. Use 'launch', 'quit', or 'focus'."


async def _handle_app_async(args: dict) -> str:
    budget = _shared_operation_budget(args, 5.0)

    async def run_app_action() -> str:
        try:
            result = await _run_native_mutation(
                lambda: _handle_app(args),
                budget=budget,
                operation="native application action",
                worker=system_worker,
            )
            if not result.startswith("ERROR:"):
                _invalidate_native_mutation_state()
            return result
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state()
            return (
                "ERROR: OUTCOME_UNKNOWN: the application action may have completed; "
                "refresh application state before retrying."
            )

    return await coordinator.execute_foreground(
        run_app_action,
        budget=budget,
        operation_manages_deadline=True,
    )


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

    subtree = native_adapter.get_subtree(element, max_depth=max_depth)
    if subtree is None:
        return f"ERROR: Could not expand subtree for [{element_id}]."

    registry.register_tree(subtree, pid=element.pid)
    text = subtree.to_text(max_depth=max_depth)

    return (
        f"Subtree of [{element_id}] ({_count_interactive(subtree)} interactive elements):\n\n"
        f"{text}\n\n"
        f"Use [id] numbers with click or type to interact."
    )


async def _handle_get_subtree_async(args: dict) -> str:
    element_id = args.get("id")
    if element_id is None:
        return "ERROR: id is required."
    snapshot_token = str(args.get("snapshot", "")).strip()
    if not snapshot_token:
        return "ERROR: snapshot is required. Call tree first."
    if not native_adapter:
        return "ERROR: No native adapter available."

    try:
        source_snapshot, record = coordinator.observations.resolve_with_snapshot(
            snapshot_token,
            element_id,
            expected_provider="native",
            expected_mode=OperationMode.FOREGROUND,
        )
    except OperationError as exc:
        return f"ERROR: subtree [{element_id}]: {exc}"
    element = record.value
    if not isinstance(element, UIElement):
        return f"ERROR: subtree [{element_id}]: snapshot record is not a UI element."
    if element.platform_ref is None:
        return f"ERROR: Element [{element_id}] has no native reference for subtree expansion."

    max_depth = max(0, min(int(args.get("max_depth", 5)), 15))
    budget = OperationBudget.start(5.0)

    def load_subtree() -> tuple[bool, UIElement | None]:
        if (
            hasattr(native_adapter, "is_element_valid")
            and element.source == "native"
            and not native_adapter.is_element_valid(element)
        ):
            return False, None
        return True, native_adapter.get_subtree(element, max_depth=max_depth)

    is_valid, subtree = await native_worker.run(
        load_subtree,
        budget=budget,
        operation="native subtree query",
    )
    if not is_valid:
        return f"ERROR: Element [{element_id}] is stale. Call tree to refresh."
    if subtree is None:
        return f"ERROR: Could not expand subtree for [{element_id}]."

    pid = element.pid
    if not pid and source_snapshot.target_id.startswith("pid:"):
        try:
            pid = int(source_snapshot.target_id.removeprefix("pid:"))
        except ValueError:
            pid = 0
    registry.register_tree(subtree, pid=pid)
    token, snapshot_count, snapshot_ids = _snapshot_native_tree(subtree, pid)
    text = _render_snapshot_tree(subtree, snapshot_ids, max_depth=max_depth)
    return (
        f"snapshot={token}\n"
        f"Subtree of [{element_id}] ({_count_interactive(subtree)} interactive elements; "
        f"{snapshot_count} elements):\n\n{text}\n\n"
        "Use this snapshot with [id] numbers for click or type."
    )


async def _handle_window(args: dict) -> str:
    action = args.get("action", "").lower()
    pid = args.get("pid")
    budget = _shared_operation_budget(args, 5.0)

    if action != "list" and not args.get("_foreground_coordinator_locked", False):
        internal = _coordinator_args(
            args,
            budget=budget,
            marker="_foreground_coordinator_locked",
        )
        return await coordinator.execute_foreground(
            lambda: _handle_window(internal),
            budget=budget,
            operation_manages_deadline=True,
        )

    if action == "list":
        if sys.platform != "darwin":
            return "ERROR: Window listing currently supports macOS only."
        if not native_adapter:
            return "ERROR: No native adapter available."

        def list_windows() -> list[tuple[object, UIElement]]:
            found: list[tuple[object, UIElement]] = []
            for app_info in native_adapter.list_apps()[:100]:
                try:
                    windows = native_adapter.get_browser_trees(
                        app_info.pid,
                        max_depth=0,
                    )
                except Exception:
                    continue
                for window_element in windows[:100]:
                    found.append((app_info, window_element))
            return found

        try:
            windows = await native_worker.run(
                list_windows,
                budget=budget,
                operation="native window inventory",
            )
        except Exception as exc:
            logger.debug("Window inventory failed (%s)", type(exc).__name__)
            return "ERROR: Window inventory failed."

        if not windows:
            return "No windows found."
        lines = ["Native windows (use snapshot + id for every mutation):"]
        for app_info, window_element in windows:
            token, _count, _ids = _snapshot_native_tree(
                window_element,
                app_info.pid,
            )
            bounds = window_element.bounds
            geometry = ""
            if bounds is not None:
                x, y, width, height = bounds
                geometry = f" pos=({x},{y}) size={width}x{height}"
            title = " ".join(window_element.name.split())[:160]
            lines.append(
                f"snapshot={token} id={window_element.id} PID {app_info.pid} | "
                f"{app_info.name} | \"{title}\"{geometry}"
            )
        return "\n".join(lines)

    if pid is None:
        return "ERROR: pid is required for this action."
    if not native_adapter:
        return "ERROR: No native adapter available."

    snapshot_token = str(args.get("snapshot", "")).strip()
    window_id = args.get("id")
    if not snapshot_token or window_id is None:
        return (
            "ERROR: snapshot and id from window(action='list') are required for "
            "this action."
        )
    try:
        source_snapshot, record = coordinator.observations.resolve_with_snapshot(
            snapshot_token,
            int(window_id),
            expected_provider="native",
            expected_mode=OperationMode.FOREGROUND,
        )
    except (OperationError, TypeError, ValueError) as exc:
        return f"ERROR: window target is stale or invalid: {exc}"
    if source_snapshot.target_id != f"pid:{pid}":
        return "ERROR: TARGET_MISMATCH: pid does not own the requested window snapshot."
    window_element = record.value
    if not isinstance(window_element, UIElement):
        return "ERROR: TARGET_MISMATCH: snapshot record is not a native window."
    role = "".join(character for character in window_element.role.casefold() if character.isalnum())
    if role not in {"window", "dialog", "sheet"}:
        return "ERROR: TARGET_MISMATCH: requested element is not a window."
    if window_element.platform_ref is None:
        return "ERROR: Native window reference is unavailable."
    valid = await native_worker.run(
        lambda: native_adapter.is_element_valid(window_element),
        budget=budget,
        operation="native window validity check",
    )
    if not valid:
        return "ERROR: Native window target is stale. Call window(action='list') again."
    window = window_element.platform_ref

    ax_value_create = point_type = size_type = point_factory = size_factory = None
    if action in {"move", "resize"}:
        def load_geometry_types():
            from ApplicationServices import (
                AXValueCreate,
                kAXValueCGPointType,
                kAXValueCGSizeType,
            )
            from Quartz import CGPoint, CGSize

            return (
                AXValueCreate,
                kAXValueCGPointType,
                kAXValueCGSizeType,
                CGPoint,
                CGSize,
            )

        try:
            (
                ax_value_create,
                point_type,
                size_type,
                point_factory,
                size_factory,
            ) = await native_worker.run(
                load_geometry_types,
                budget=budget,
                operation="native window geometry types",
            )
        except Exception as exc:
            logger.debug("Window geometry provider failed (%s)", type(exc).__name__)
            return "ERROR: Window geometry provider is unavailable."

    if action == "focus":
        input_backend = _input_backend
        action_state: ProviderCallState | None = None
        try:
            available = bool(
                input_backend is not None
                and await input_worker.run(
                    input_backend.is_available,
                    budget=budget,
                    operation="window focus capability check",
                )
            )
            if available:
                focus_ok, focus_err = await _verify_focus(pid, budget=budget)
                if not focus_ok:
                    return f"ERROR: {focus_err}."

            action_state = ProviderCallState()
            result = await run_native_action_until(
                pid,
                lambda: native_adapter.focus_window(window_element),
                lambda: native_adapter.is_window_focused(window_element),
                budget=budget,
                action_worker=native_worker,
                condition_worker=native_worker,
                action_state=action_state,
            )
            if not result.condition_met:
                return "ERROR: Native accessibility provider could not verify exact window focus."
            _invalidate_native_mutation_state(pid=pid)
            return f"Focused window [{window_id}] for PID {pid}."
        except (OperationError, asyncio.CancelledError):
            if action_state is None or not action_state.may_have_run:
                raise
            coordinator.poison_foreground_until(native_worker.wait_until_idle())
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: window focus may have completed; "
                "refresh window state before retrying."
            )
        except Exception as exc:
            logger.debug("Window focus failed (%s)", type(exc).__name__)
            return "ERROR: Window focus failed."

    elif action == "minimize":
        try:
            ax_error = await _run_native_mutation(
                lambda: native_adapter._ax.AXUIElementSetAttributeValue(
                    window,
                    "AXMinimized",
                    True,
                ),
                budget=budget,
                operation="native window minimize",
                worker=native_worker,
            )
            if ax_error != 0:
                return "ERROR: Native accessibility provider rejected window minimization."
            _invalidate_native_mutation_state(pid=pid)
            return f"Minimized window [{window_id}] for PID {pid}."
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: window minimization may have completed; "
                "refresh window state before retrying."
            )
        except (OperationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.debug("Window minimize failed (%s)", type(exc).__name__)
            return "ERROR: Window minimize failed."

    elif action == "close":
        try:
            close_button = await native_worker.run(
                lambda: native_adapter._read_attr(window, "AXCloseButton"),
                budget=budget,
                operation="native window close-button lookup",
            )
            if close_button:
                ax_error = await _run_native_mutation(
                    lambda: native_adapter._ax.AXUIElementPerformAction(
                        close_button,
                        "AXPress",
                    ),
                    budget=budget,
                    operation="native window close",
                    worker=native_worker,
                )
                if ax_error != 0:
                    return "ERROR: Native accessibility provider rejected window close."
                _invalidate_native_mutation_state(pid=pid)
                return f"Closed window for PID {pid}."
            return "ERROR: No close button found."
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: window close may have completed; "
                "refresh window state before retrying."
            )
        except (OperationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.debug("Window close failed (%s)", type(exc).__name__)
            return "ERROR: Window close failed."

    elif action == "move":
        x = args.get("x")
        y = args.get("y")
        if x is None or y is None:
            return "ERROR: x and y are required for move."
        try:
            ax_error = await _run_native_mutation(
                lambda: native_adapter._ax.AXUIElementSetAttributeValue(
                    window,
                    "AXPosition",
                    ax_value_create(
                        point_type,
                        point_factory(float(x), float(y)),
                    ),
                ),
                budget=budget,
                operation="native window move",
                worker=native_worker,
            )
            if ax_error != 0:
                return "ERROR: Native accessibility provider rejected window movement."
            _invalidate_native_mutation_state(pid=pid)
            return f"Moved window to ({x}, {y})."
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: window movement may have completed; "
                "refresh window state before retrying."
            )
        except (OperationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.debug("Window move failed (%s)", type(exc).__name__)
            return "ERROR: Window move failed."

    elif action == "resize":
        width = args.get("width")
        height = args.get("height")
        if width is None or height is None:
            return "ERROR: width and height are required for resize."
        try:
            ax_error = await _run_native_mutation(
                lambda: native_adapter._ax.AXUIElementSetAttributeValue(
                    window,
                    "AXSize",
                    ax_value_create(
                        size_type,
                        size_factory(float(width), float(height)),
                    ),
                ),
                budget=budget,
                operation="native window resize",
                worker=native_worker,
            )
            if ax_error != 0:
                return "ERROR: Native accessibility provider rejected window resize."
            _invalidate_native_mutation_state(pid=pid)
            return f"Resized window to {width}x{height}."
        except _NativeMutationOutcomeUnknown:
            _invalidate_native_mutation_state(pid=pid)
            return (
                "ERROR: OUTCOME_UNKNOWN: window resize may have completed; "
                "refresh window state before retrying."
            )
        except (OperationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.debug("Window resize failed (%s)", type(exc).__name__)
            return "ERROR: Window resize failed."

    return f"ERROR: Unknown action '{action}'."


def _handle_context(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    apps = native_adapter.list_apps()
    frontmost = next((a for a in apps if a.is_frontmost), None)

    if not frontmost:
        apps_summary = ", ".join(f"{a.name}({a.pid})" for a in apps[:5])
        return f"no frontmost app | running: {apps_summary}"

    app_label = frontmost.name
    window_label = f" — {frontmost.windows[0]}" if frontmost.windows else ""

    focused = native_adapter.get_focused_element()
    if focused:
        registry.register_element(focused)
        focused_label = f" | [{focused.id}] {focused.role} \"{focused.name}\" focused"
    else:
        focused_label = ""

    return f"{app_label}{window_label}{focused_label}"


async def _handle_context_async(args: dict) -> str:
    if not native_adapter:
        return "ERROR: No native adapter available."

    def load_context():
        apps = native_adapter.list_apps()
        frontmost = next((app for app in apps if app.is_frontmost), None)
        focused = native_adapter.get_focused_element() if frontmost else None
        return apps, frontmost, focused

    apps, frontmost, focused = await native_worker.run(
        load_context,
        budget=OperationBudget.start(5.0),
        operation="native context query",
    )
    if not frontmost:
        apps_summary = ", ".join(f"{app.name}({app.pid})" for app in apps[:5])
        return f"no frontmost app | running: {apps_summary}"

    app_label = frontmost.name
    window_label = f" — {frontmost.windows[0]}" if frontmost.windows else ""
    if focused is None:
        return f"{app_label}{window_label}"

    if not focused.pid:
        focused.pid = frontmost.pid
    registry.register_element(focused)
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id=f"pid:{frontmost.pid}",
        generation=0,
        revision=time.monotonic_ns(),
        elements=[ElementRecord(local_id=focused.id, value=focused)],
    )
    return (
        f"snapshot={snapshot.token}\n"
        f'{app_label}{window_label} | [{focused.id}] {focused.role} '
        f'"{focused.name}" focused'
    )


# ── Shadow (background browser) handler ─────────────────────────────
def _handle_shadow(args: dict) -> str | _AppleShadowResult:
    action = args.get("action", "")
    text = args.get("text", "")
    selector = args.get("selector", "")
    direction = args.get("direction", "down")
    amount = args.get("amount", 300)

    if sys.platform != "darwin":
        return "ERROR: Shadow mode currently supports macOS only."

    if _as is None or not _as.is_available():
        return "ERROR: The optional Chromium shadow provider is unavailable."

    target_id, target_error = _require_shadow_target_id(
        args,
        "Apple Events shadow action",
    )
    if target_error:
        return target_error
    target, target_error = _resolve_applescript_target(target_id)
    if target_error:
        return target_error
    target_args = {
        "tab_index": target.index,
        "window_index": target.window_index,
        "tab_id": target.id,
        "window_id": target.window_id,
    }

    if action == "click":
        if not text and not selector:
            return "ERROR: 'text' or 'selector' required for click."
        if selector:
            outcome = _as.shadow_click_outcome(selector, **target_args)
        else:
            outcome = _as.shadow_click_by_text_outcome(text, **target_args)
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return _AppleShadowResult("ERROR: Apple Events click was not dispatched.")
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return _AppleShadowResult(
                "ERROR: OUTCOME_UNKNOWN: Apple Events click may have completed.",
                invalidate_target=True,
            )
        clicked = bool(outcome.value and outcome.value != "not found")
        return _AppleShadowResult(
            "Shadow click completed." if clicked else "ERROR: Shadow click target not found.",
            invalidate_target=clicked,
        )

    elif action == "type":
        if not text:
            return "ERROR: 'text' required for type."
        outcome = _as.shadow_type_outcome(text, selector=selector, **target_args)
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return _AppleShadowResult("ERROR: Apple Events input was not dispatched.")
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return _AppleShadowResult(
                "ERROR: OUTCOME_UNKNOWN: Apple Events input may have completed.",
                invalidate_target=True,
            )
        typed = outcome.value == "typed"
        return _AppleShadowResult(
            f"Shadow typed {len(text)} characters"
            if typed
            else "ERROR: Shadow input was not confirmed.",
            invalidate_target=typed,
        )

    elif action == "press_key":
        key = text or "Enter"
        outcome = _as.shadow_press_key_outcome(key, **target_args)
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return _AppleShadowResult("ERROR: Apple Events key input was not dispatched.")
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return _AppleShadowResult(
                "ERROR: OUTCOME_UNKNOWN: Apple Events key input may have completed.",
                invalidate_target=True,
            )
        pressed = outcome.value == "pressed"
        return _AppleShadowResult(
            "Shadow key input completed."
            if pressed
            else "ERROR: Shadow key input was not confirmed.",
            invalidate_target=pressed,
        )

    elif action == "scroll":
        outcome = _as.shadow_scroll_outcome(
            direction=direction,
            amount=amount,
            selector=selector,
            **target_args,
        )
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return _AppleShadowResult("ERROR: Apple Events scroll was not dispatched.")
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return _AppleShadowResult(
                "ERROR: OUTCOME_UNKNOWN: Apple Events scroll may have completed.",
                invalidate_target=True,
            )
        scrolled = outcome.value == "scrolled"
        return _AppleShadowResult(
            "Shadow scroll completed."
            if scrolled
            else "ERROR: Shadow scroll target not found.",
            invalidate_target=scrolled,
        )

    elif action == "read":
        result = _as.shadow_read_interactive(**target_args)
        if result:
            return f"Interactive elements (background scan):\n\n{result}"
        return "No interactive elements found or the shadow provider is unavailable."

    elif action == "js":
        if not text:
            return "ERROR: 'text' (JS code) required for js action."
        outcome = _as.shadow_execute_js_outcome(text, **target_args)
        if outcome.status is _as.ShadowExecutionStatus.NOT_DISPATCHED:
            return _AppleShadowResult("ERROR: Apple Events JavaScript was not dispatched.")
        if outcome.status is _as.ShadowExecutionStatus.OUTCOME_UNKNOWN:
            return _AppleShadowResult(
                "ERROR: OUTCOME_UNKNOWN: Apple Events JavaScript may have executed.",
                invalidate_target=True,
            )
        return _AppleShadowResult(
            _javascript_result_summary(outcome.value),
            invalidate_target=True,
        )

    return f"ERROR: Unknown action '{action}'."


async def _handle_shadow_async(args: dict) -> str:
    target_id, target_error = _require_shadow_target_id(
        args,
        "Apple Events shadow action",
    )
    if target_error:
        return target_error
    budget = _shared_operation_budget(args, 10.0)

    async def run_apple_action() -> str:
        action = str(args.get("action", ""))
        if action == "read":
            return await apple_worker.run(
                lambda: _handle_shadow(args),
                budget=budget,
                operation="Apple Events shadow read",
            )
        try:
            result = await _run_native_mutation(
                lambda: _handle_shadow(args),
                budget=budget,
                operation="Apple Events shadow action",
                worker=apple_worker,
            )
        except _NativeMutationOutcomeUnknown:
            _invalidate_apple_mutation_state(target_id)
            return (
                "ERROR: OUTCOME_UNKNOWN: the Apple Events action may have completed; "
                "refresh list_tabs/web_tree before retrying."
            )
        if isinstance(result, _AppleShadowResult):
            if result.invalidate_target:
                _invalidate_apple_mutation_state(target_id)
            return result.text
        return result

    return await coordinator.execute_shadow(
        target_id,
        run_apple_action,
        budget=budget,
        operation_manages_deadline=True,
    )


# ── Shadow DOM pierce handler ────────────────────────────────────────
async def _handle_pierce(args: dict) -> str:
    selector = args.get("selector", "")
    if not selector:
        return "ERROR: pierce requires selector."
    if not args.get("shadow", False):
        return "ERROR: Shadow-DOM protocol inspection requires explicit shadow=true."
    target_id, target_error = _require_shadow_target_id(args, "pierce")
    if target_error:
        return target_error
    budget = OperationBudget.start(5.0)
    session, tab, provider_error = await _get_cdp_session(args, budget=budget)
    if session is None or tab is None:
        return provider_error or "ERROR: Persistent shadow DOM provider is unavailable."
    try:
        revision_before = await budget.wait_for(
            _persistent_document_revision(session),
            operation="pierce document revision",
        )
        pierced = await coordinator.observe(
            (
                "pierce",
                target_id,
                session.generation,
                revision_before,
                selector,
            ),
            lambda: session.pierce_selector(selector),
            budget=budget,
        )
        revision_after = await budget.wait_for(
            _persistent_document_revision(session),
            operation="pierce document revision",
        )
        if revision_before != revision_after:
            return "ERROR: Shadow document changed during pierce; retry."
        merged = merge_pierced_nodes(pierced, set())
        if not merged:
            return "No interactive shadow DOM elements matched the selector."

        records: list[ElementRecord] = []
        lines: list[str] = []
        registry_elements: list[UIElement] = []
        for shadow in merged[:500]:
            actionable = bool(shadow.get("actionable", False))
            element = UIElement(
                id=cdp_client._next_id(),
                role=shadow["role"],
                name=shadow["name"],
                states=[] if actionable else ["read-only"],
                actions=["click", "focus"] if actionable else [],
                source="shadow-dom",
                platform_ref=shadow.get("backendNodeId"),
            )
            registry_elements.append(element)
            records.append(
                ElementRecord(
                    local_id=element.id,
                    value=element,
                    actionable=actionable,
                )
            )
            lines.append(element.to_flat_line())
        registry.register_elements(registry_elements)
        snapshot = coordinator.observations.create(
            provider="cdp-persistent",
            mode=OperationMode.SHADOW,
            target_id=target_id,
            generation=session.generation,
            revision=revision_after,
            elements=records,
        )
        return f"snapshot={snapshot.token}\n" + "\n".join(lines)
    except OperationError as exc:
        return f"ERROR: pierce: {exc}"
    except Exception as exc:
        logger.debug("Shadow DOM pierce failed (%s)", type(exc).__name__)
        return "ERROR: Shadow DOM pierce provider failed."


# ── Entry point ─────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO)
    logger.info("agent-eyes starting — platform: %s", sys.platform)
    logger.info("Native and input providers will load on the first capability request")

    asyncio.run(_run())


async def _run():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        cleanup = asyncio.create_task(
            _shutdown_runtime(),
            name="agent-eyes-runtime-shutdown",
        )
        await asyncio.shield(cleanup)


async def _shutdown_runtime() -> None:
    """Close observation, browser, and provider-owned runtime resources."""
    results = list(await asyncio.gather(coordinator.close(), return_exceptions=True))
    results.extend(
        await asyncio.gather(
            cdp_pool.disconnect(),
            native_worker.aclose(),
            input_worker.aclose(),
            apple_worker.aclose(),
            system_worker.aclose(),
            return_exceptions=True,
        )
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.debug(
                "Runtime shutdown step failed (exception_type=%s)",
                type(result).__name__,
            )


if __name__ == "__main__":
    main()
