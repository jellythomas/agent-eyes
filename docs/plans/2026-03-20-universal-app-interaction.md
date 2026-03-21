# Universal App Interaction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make agent-eyes interact with ANY application (native, Electron, CEF, WebKit) by fixing the accessibility tree depth bug for web-content apps, adding OCR vision fallback, and supporting coordinate-based clicks.

**Architecture:** Two-phase approach. Phase 1 fixes the core adapter: call `force_browser_accessibility()` for all apps, dynamically extend depth when `AXWebArea` is found, raise element cap to 1000, and auto-retry shallow trees. Phase 2 adds an OCR vision fallback (`eyes_get_ocr_hints`) with platform-native OCR engines and coordinate-click support. No app classification engine needed — runtime AXWebArea detection is simpler and universal.

**Tech Stack:** Python 3.10+, PyObjC (macOS), pywinauto (Windows), pyatspi2 (Linux), Vision framework (macOS OCR), pytesseract (Linux OCR)

---

## Phase 1: Fix The Adapter

### Task 1: Universal `force_browser_accessibility()` for all apps

**Why:** Currently only called when `is_browser=True`. Electron/CEF/WebKit apps never get this call, so Chromium-based apps don't build their accessibility tree. Validated: safe on native apps (returns kAXErrorNotImplemented, no side effects).

**Files:**
- Modify: `src/agent_eyes/adapters/macos.py:291-299`

**Step 1: Modify `get_tree()` to always call `force_browser_accessibility()`**

Change `macos.py` lines 291-299 from:

```python
def get_tree(self, pid: int, max_depth: int = 5,
             is_browser: bool = False) -> UIElement | None:
    self._load()
    self.reset_ids()
    ax_app = self._ax.AXUIElementCreateApplication(pid)

    # Force browser to build full accessibility tree
    if is_browser:
        self.force_browser_accessibility(pid)
```

To:

```python
def get_tree(self, pid: int, max_depth: int = 5,
             is_browser: bool = False) -> UIElement | None:
    self._load()
    self.reset_ids()
    ax_app = self._ax.AXUIElementCreateApplication(pid)

    # Force ALL apps to enable accessibility tree construction.
    # Safe no-op on native apps (returns kAXErrorNotImplemented).
    # Critical for Electron/CEF/WebKit apps that lazily build AX trees.
    self.force_browser_accessibility(pid)
```

**Step 2: Verify the change loads correctly**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.adapters.macos import MacOSAdapter; print('OK')"`

Expected: `OK`

**Step 3: Manual test with Pritunl**

Run: `uv pip install -e . && /mcp` (reconnect MCP server), then call `eyes_get_tree` with PID of Pritunl.

**Step 4: Commit**

```bash
git add src/agent_eyes/adapters/macos.py
git commit -m "fix: call force_browser_accessibility() for ALL apps, not just browsers

Safe no-op on native apps. Critical for Electron/CEF/WebKit apps
that lazily build accessibility trees."
```

---

### Task 2: Dynamic depth extension on AXWebArea detection

**Why:** Electron apps have AXWebArea at depth 5-8, but interactive elements (buttons, inputs) are at depth 9-30. Current `max_depth=5` for non-browsers cuts off before reaching content. Fix: increase default to 10, and when AXWebArea is found, extend remaining depth by +10.

**Files:**
- Modify: `src/agent_eyes/server.py:636-643` (depth defaults)
- Modify: `src/agent_eyes/server.py:95-120` (tool description)
- Modify: `src/agent_eyes/adapters/macos.py:169-177` (dynamic depth extension)

**Step 1: Update depth defaults in `server.py`**

Change `server.py` lines 636-643 from:

```python
is_browser = _pu.is_browser_pid(pid)

# Browsers need deeper traversal to reach web elements inside AXWebArea.
# Native apps: default 5, max 10.  Browsers: default 15, max 20.
if is_browser:
    max_depth = min(args.get("max_depth", 15), 20)
else:
    max_depth = min(args.get("max_depth", 5), 10)
```

To:

```python
is_browser = _pu.is_browser_pid(pid)

# Universal depth: default 10, max 20.
# AXWebArea (Electron/CEF/WebKit) appears at depth 5-8.
# Dynamic extension in the adapter adds +10 when web content is found.
max_depth = min(args.get("max_depth", 10), 20)
```

**Step 2: Update tool description in `server.py`**

Change `server.py` lines 95-120 — update the `eyes_get_tree` tool description and schema:

```python
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
```

**Step 3: Add dynamic depth extension in `macos.py`**

Change `macos.py` line 169 — add `web_depth_budget` parameter and extension logic:

```python
def _element_to_ui(self, ax_el, depth: int, max_depth: int,
                   in_web_area: bool = False) -> UIElement | None:
    """Convert an AXUIElement to our UIElement format.

    Uses AXUIElementCopyMultipleAttributeValues for ~5x faster traversal
    by batching attribute reads into a single IPC round-trip per element.
    """
    if depth > max_depth:
        return None
    if self._id_counter >= self._MAX_ELEMENTS:
        return None

    # Single IPC call for all attributes (~1.3ms vs ~11ms for individual calls)
    attrs = self._batch_read_attrs(ax_el)

    role_raw = attrs.get("AXRole") or ""
    role = str(role_raw).replace("AX", "").lower() if role_raw else ""

    # Skip ignored/invisible elements
    if role in ("unknown", ""):
        return None

    # Detect entry into web content — dynamically extend depth
    if role == "webarea":
        in_web_area = True
        # Web content needs deeper traversal. Extend depth budget by 10
        # from current position (buttons are 3-5 levels below AXWebArea).
        max_depth = max(max_depth, depth + 10)
```

This replaces lines 169-193 (everything up to and including the `in_web_area = True` line at 193). The rest of the method stays the same.

**Step 4: Apply the same dynamic depth extension to `windows.py`**

In `windows.py` `_wrapper_to_ui()` method (around line 100), add after the `if role.lower() == "document":` block:

```python
if role.lower() == "document":
    in_web_area = True
    max_depth = max(max_depth, depth + 10)
```

**Step 5: Apply the same dynamic depth extension to `linux.py`**

In `linux.py` `_atspi_to_ui()` method (around line 143), add after the web area detection:

```python
if role in ("document web", "document frame"):
    in_web_area = True
    max_depth = max(max_depth, depth + 10)
```

**Step 6: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.server import _handle_get_tree; print('OK')"`

Expected: `OK`

**Step 7: Commit**

```bash
git add src/agent_eyes/server.py src/agent_eyes/adapters/macos.py src/agent_eyes/adapters/windows.py src/agent_eyes/adapters/linux.py
git commit -m "feat: dynamic depth extension when AXWebArea detected

- Default max_depth 5→10, max allowed 10→20 for ALL apps
- When traversal finds AXWebArea/document, extends depth by +10
- Applies to all platforms (macOS, Windows, Linux)
- Fixes: Electron/CEF/WebKit apps show empty groups at depth 5"
```

---

### Task 3: Raise element cap and add per-attribute timeout

**Why:** VS Code has 683 elements (exceeds 500 cap). Traversal at 683 elements takes only 0.17s, so 1000 is safe. Per-attribute timeout prevents hangs on unresponsive apps. Windows/Linux adapters currently have no cap.

**Files:**
- Modify: `src/agent_eyes/adapters/macos.py:167` (raise cap)
- Modify: `src/agent_eyes/adapters/macos.py:152-164` (add timeout to batch read)
- Modify: `src/agent_eyes/adapters/windows.py` (add cap)
- Modify: `src/agent_eyes/adapters/linux.py` (add cap)

**Step 1: Raise macOS cap from 500 to 1000**

Change `macos.py` line 167:

```python
_MAX_ELEMENTS = 1000
```

**Step 2: Add per-attribute timeout to macOS batch read**

Add a 2-second timeout wrapper around `_batch_read_attrs` in `macos.py`. Add this method before `_batch_read_attrs` (around line 134):

```python
def _read_with_timeout(self, func, *args, timeout_sec: float = 2.0):
    """Call an AX function with a timeout to prevent hangs on unresponsive apps."""
    import threading
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func(*args)
        except Exception as e:
            error[0] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        logger.warning("AX attribute read timed out after %.1fs", timeout_sec)
        return None
    if error[0]:
        raise error[0]
    return result[0]
```

Then wrap the batch read call in `_element_to_ui` (line 182):

```python
# Single IPC call for all attributes (~1.3ms vs ~11ms for individual calls)
attrs = self._batch_read_attrs(ax_el)
if attrs is None:
    return None
```

And update `_batch_read_attrs` to handle timeouts internally by catching exceptions.

**Step 3: Add element cap to `windows.py`**

Add at class level in `WindowsAdapter` (before `_wrapper_to_ui`):

```python
_MAX_ELEMENTS = 1000

def reset_ids(self):
    self._id_counter = 0

def _next_id(self):
    self._id_counter += 1
    return self._id_counter
```

And add the cap check at the top of `_wrapper_to_ui`:

```python
def _wrapper_to_ui(self, wrapper, depth: int, max_depth: int,
                   in_web_area: bool = False) -> UIElement | None:
    if depth > max_depth:
        return None
    if self._id_counter >= self._MAX_ELEMENTS:
        return None
```

Update `get_tree` to call `self.reset_ids()` at the start.

**Step 4: Add element cap to `linux.py`**

Same pattern as Windows — add `_MAX_ELEMENTS = 1000`, `reset_ids()`, `_next_id()`, and the cap check at the top of `_atspi_to_ui`.

Update `get_tree` to call `self.reset_ids()` at the start.

**Step 5: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.adapters.macos import MacOSAdapter; from agent_eyes.adapters.windows import WindowsAdapter; from agent_eyes.adapters.linux import LinuxAdapter; print('OK')"`

Expected: `OK` (Windows/Linux may fail import on macOS — that's fine, verify syntax)

**Step 6: Commit**

```bash
git add src/agent_eyes/adapters/macos.py src/agent_eyes/adapters/windows.py src/agent_eyes/adapters/linux.py
git commit -m "perf: raise element cap 500→1000, add timeout protection

- macOS: _MAX_ELEMENTS 500→1000 (VS Code needs 683, traversal 0.17s)
- macOS: 2s per-attribute timeout prevents hangs on unresponsive apps
- Windows/Linux: add _MAX_ELEMENTS=1000 cap (was uncapped)"
```

---

### Task 4: Auto-retry with deeper depth when web content is sparse

**Why:** Currently `_tree_has_web_content()` only appends a warning text. Should auto-retry with `max_depth=20` when AXWebArea is found but tree has few interactive elements.

**Files:**
- Modify: `src/agent_eyes/server.py:629-679` (auto-retry logic)
- Modify: `src/agent_eyes/server.py:682-688` (add interactive element counter)

**Step 1: Add interactive element counter helper**

Add after `_tree_has_web_content` (after line 688):

```python
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
```

**Step 2: Update `_handle_get_tree` with auto-retry logic**

Replace `server.py` lines 629-679 with:

```python
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
    meta += f"):"

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
```

**Step 3: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.server import _handle_get_tree; print('OK')"`

Expected: `OK`

**Step 4: Commit**

```bash
git add src/agent_eyes/server.py
git commit -m "feat: auto-retry with deeper depth when web content is sparse

- Count interactive elements after building tree
- If AXWebArea found but <5 interactive elements, auto-rebuild at depth 20
- Add tree quality metadata (element count, web content detected)
- Advisory hint for eyes_get_ocr_hints when tree is insufficient"
```

---

## Phase 2: OCR Vision Fallback

### Task 5: Window screenshot capture utility

**Why:** OCR needs a screenshot of the target window. Platform-native capture APIs give us per-window screenshots with minimal overhead.

**Files:**
- Create: `src/agent_eyes/screenshot.py`

**Step 1: Create the screenshot module**

Create `src/agent_eyes/screenshot.py`:

```python
"""Cross-platform window screenshot capture for agent-eyes.

Platform support:
  - macOS:   CGWindowListCreateImage via PyObjC (requires Screen Recording permission)
  - Windows: PrintWindow via pywin32
  - Linux:   import (ImageMagick) via subprocess
"""
from __future__ import annotations

import sys
import logging
from dataclasses import dataclass

logger = logging.getLogger("agent-eyes")


@dataclass
class WindowCapture:
    """Raw screenshot data for a window."""
    image_data: bytes       # PNG image data
    width: int              # Image width in pixels
    height: int             # Image height in pixels
    scale_factor: float     # Backing scale factor (2.0 on Retina)
    window_x: int           # Window origin X in screen points
    window_y: int           # Window origin Y in screen points
    window_w: int           # Window width in screen points
    window_h: int           # Window height in screen points


def capture_window(pid: int) -> WindowCapture | None:
    """Capture a screenshot of the topmost window for the given PID."""
    if sys.platform == "darwin":
        return _capture_macos(pid)
    elif sys.platform == "win32":
        return _capture_windows(pid)
    elif sys.platform == "linux":
        return _capture_linux(pid)
    return None


def _capture_macos(pid: int) -> WindowCapture | None:
    """macOS: CGWindowListCopyWindowInfo + CGWindowListCreateImage."""
    try:
        import Quartz
        from AppKit import NSBitmapImageRep, NSPNGFileType
    except ImportError:
        logger.error("pyobjc-framework-Quartz required for macOS screenshots")
        return None

    # Find the window ID for this PID
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    target_window = None
    for win_info in window_list:
        if win_info.get(Quartz.kCGWindowOwnerPID) == pid:
            layer = win_info.get(Quartz.kCGWindowLayer, 999)
            if layer == 0:  # Normal window layer
                target_window = win_info
                break

    if target_window is None:
        logger.error("No window found for PID %d", pid)
        return None

    window_id = target_window[Quartz.kCGWindowNumber]
    bounds = target_window[Quartz.kCGWindowBounds]
    win_x = int(bounds["X"])
    win_y = int(bounds["Y"])
    win_w = int(bounds["Width"])
    win_h = int(bounds["Height"])

    # Capture the window
    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,  # Minimum bounding rect
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
    )
    if cg_image is None:
        logger.error("CGWindowListCreateImage returned None (Screen Recording permission?)")
        return None

    img_w = Quartz.CGImageGetWidth(cg_image)
    img_h = Quartz.CGImageGetHeight(cg_image)
    scale = img_w / win_w if win_w > 0 else 1.0

    # Convert to PNG data
    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, {})
    if png_data is None:
        return None

    return WindowCapture(
        image_data=bytes(png_data),
        width=img_w,
        height=img_h,
        scale_factor=scale,
        window_x=win_x,
        window_y=win_y,
        window_w=win_w,
        window_h=win_h,
    )


def _capture_windows(pid: int) -> WindowCapture | None:
    """Windows: PrintWindow via pywin32."""
    try:
        import win32gui
        import win32ui
        import win32process
        import win32con
    except ImportError:
        logger.error("pywin32 required for Windows screenshots")
        return None

    # Find HWND for PID
    target_hwnd = None
    def enum_callback(hwnd, _):
        nonlocal target_hwnd
        if win32gui.IsWindowVisible(hwnd):
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == pid:
                target_hwnd = hwnd
                return False  # Stop enumeration
        return True

    win32gui.EnumWindows(enum_callback, None)
    if target_hwnd is None:
        return None

    rect = win32gui.GetWindowRect(target_hwnd)
    win_w = rect[2] - rect[0]
    win_h = rect[3] - rect[1]

    # Capture via PrintWindow
    hwnd_dc = win32gui.GetWindowDC(target_hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, win_w, win_h)
    save_dc.SelectObject(bitmap)

    # PW_RENDERFULLCONTENT = 0x00000002
    import ctypes
    ctypes.windll.user32.PrintWindow(target_hwnd, save_dc.GetSafeHdc(), 0x00000002)

    # Convert to PNG
    import io
    from PIL import Image
    bmp_info = bitmap.GetInfo()
    bmp_data = bitmap.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]), bmp_data, "raw", "BGRX", 0, 1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    # Cleanup
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(target_hwnd, hwnd_dc)
    bitmap.DeleteObject()

    return WindowCapture(
        image_data=buf.getvalue(),
        width=win_w,
        height=win_h,
        scale_factor=1.0,
        window_x=rect[0],
        window_y=rect[1],
        window_w=win_w,
        window_h=win_h,
    )


def _capture_linux(pid: int) -> WindowCapture | None:
    """Linux: xdotool + import (ImageMagick) for X11."""
    import subprocess

    # Find window ID from PID
    try:
        result = subprocess.run(
            ["xdotool", "search", "--pid", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        window_ids = result.stdout.strip().split("\n")
        if not window_ids or not window_ids[0]:
            return None
        window_id = window_ids[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Get window geometry
    try:
        result = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            capture_output=True, text=True, timeout=5,
        )
        geo = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                geo[k] = int(v)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Capture via import (ImageMagick)
    import tempfile, os
    tmp_path = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(
            ["import", "-window", window_id, tmp_path],
            timeout=10,
        )
        if not os.path.exists(tmp_path):
            return None
        with open(tmp_path, "rb") as f:
            png_data = f.read()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    win_x = geo.get("X", 0)
    win_y = geo.get("Y", 0)
    win_w = geo.get("WIDTH", 0)
    win_h = geo.get("HEIGHT", 0)

    return WindowCapture(
        image_data=png_data,
        width=win_w,
        height=win_h,
        scale_factor=1.0,
        window_x=win_x,
        window_y=win_y,
        window_w=win_w,
        window_h=win_h,
    )
```

**Step 2: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.screenshot import capture_window; print('OK')"`

Expected: `OK`

**Step 3: Manual test — capture Pritunl window**

Run: `uv run python -c "
from agent_eyes.screenshot import capture_window
cap = capture_window(1204)
if cap:
    with open('/tmp/pritunl_capture.png', 'wb') as f:
        f.write(cap.image_data)
    print(f'Captured: {cap.width}x{cap.height} scale={cap.scale_factor}')
else:
    print('FAILED — check Screen Recording permission')
"`

**Step 4: Commit**

```bash
git add src/agent_eyes/screenshot.py
git commit -m "feat: cross-platform window screenshot capture

- macOS: CGWindowListCreateImage (requires Screen Recording permission)
- Windows: PrintWindow via pywin32
- Linux: xdotool + import (ImageMagick)"
```

---

### Task 6: OCR engine abstraction

**Why:** Need platform-native OCR that returns text + bounding boxes for coordinate-based clicking. Vision framework (macOS), Windows.Media.Ocr (Windows), pytesseract (Linux).

**Files:**
- Create: `src/agent_eyes/ocr.py`
- Modify: `pyproject.toml` (add Vision framework dependency)

**Step 1: Create the OCR module**

Create `src/agent_eyes/ocr.py`:

```python
"""Cross-platform OCR engines for agent-eyes.

Returns text blocks with bounding boxes for coordinate-based interaction.
Uses platform-native APIs where available (zero extra deps on macOS/Windows).

Platform support:
  - macOS:   Apple Vision framework (VNRecognizeTextRequest)
  - Windows: Windows.Media.Ocr (UWP OCR)
  - Linux:   pytesseract (requires tesseract-ocr system package)
"""
from __future__ import annotations

import abc
import sys
import logging
from dataclasses import dataclass

logger = logging.getLogger("agent-eyes")


@dataclass
class OCRHint:
    """A text block found by OCR with its screen-space bounding box."""
    text: str
    x: int          # Screen X in points (not pixels)
    y: int          # Screen Y in points
    width: int      # Width in points
    height: int     # Height in points
    confidence: float  # 0.0 - 1.0


class OCREngine(abc.ABC):
    """Abstract OCR engine interface."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        ...

    @abc.abstractmethod
    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        """Run OCR on PNG image data. Returns text hints with screen-space coordinates."""
        ...


class MacOSOCR(OCREngine):
    """macOS OCR via Apple Vision framework."""

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Vision  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        import Vision
        import Quartz
        from Foundation import NSData

        # Load image from PNG data
        ns_data = NSData.dataWithBytes_length_(image_data, len(image_data))
        cg_source = Quartz.CGImageSourceCreateWithData(ns_data, None)
        if cg_source is None:
            return []
        cg_image = Quartz.CGImageSourceCreateImageAtIndex(cg_source, 0, None)
        if cg_image is None:
            return []

        img_w = Quartz.CGImageGetWidth(cg_image)
        img_h = Quartz.CGImageGetHeight(cg_image)

        # Run text recognition
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(1)  # 0=fast, 1=accurate
        request.setUsesLanguageCorrection_(True)

        success, error = handler.performRequests_error_([request], None)
        if not success:
            logger.error("Vision OCR failed: %s", error)
            return []

        hints = []
        for observation in request.results():
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = candidates[0].string()
            conf = candidates[0].confidence()

            # Vision returns normalized coords (0-1, origin bottom-left)
            bbox = observation.boundingBox()
            # Convert to pixel coords (origin top-left)
            px_x = bbox.origin.x * img_w
            px_y = (1.0 - bbox.origin.y - bbox.size.height) * img_h
            px_w = bbox.size.width * img_w
            px_h = bbox.size.height * img_h

            # Convert pixel coords to screen points
            screen_x = window_x + int(px_x / scale_factor)
            screen_y = window_y + int(px_y / scale_factor)
            screen_w = int(px_w / scale_factor)
            screen_h = int(px_h / scale_factor)

            hints.append(OCRHint(
                text=text,
                x=screen_x,
                y=screen_y,
                width=screen_w,
                height=screen_h,
                confidence=round(conf, 3),
            ))

        return hints


class WindowsOCR(OCREngine):
    """Windows OCR via Windows.Media.Ocr."""

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from winrt.windows.media.ocr import OcrEngine as _OcrEngine  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        # Windows OCR implementation would go here
        # Using winrt-Windows.Media.Ocr package
        logger.warning("Windows OCR not yet implemented")
        return []


class LinuxOCR(OCREngine):
    """Linux OCR via pytesseract."""

    def is_available(self) -> bool:
        if sys.platform == "darwin" or sys.platform == "win32":
            return False
        try:
            import pytesseract  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_data))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        hints = []
        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            if not text:
                continue
            conf = int(data["conf"][i])
            if conf < 30:  # Skip low-confidence detections
                continue

            px_x = data["left"][i]
            px_y = data["top"][i]
            px_w = data["width"][i]
            px_h = data["height"][i]

            screen_x = window_x + int(px_x / scale_factor)
            screen_y = window_y + int(px_y / scale_factor)
            screen_w = int(px_w / scale_factor)
            screen_h = int(px_h / scale_factor)

            hints.append(OCRHint(
                text=text,
                x=screen_x,
                y=screen_y,
                width=screen_w,
                height=screen_h,
                confidence=round(conf / 100.0, 3),
            ))

        return hints


def get_ocr_engine() -> OCREngine | None:
    """Get the best available OCR engine for the current platform."""
    if sys.platform == "darwin":
        engine = MacOSOCR()
        if engine.is_available():
            return engine
    elif sys.platform == "win32":
        engine = WindowsOCR()
        if engine.is_available():
            return engine
    else:
        engine = LinuxOCR()
        if engine.is_available():
            return engine
    return None
```

**Step 2: Add Vision framework to pyproject.toml**

Add `pyobjc-framework-Vision>=10.0` to the macOS optional dependencies:

```toml
[project.optional-dependencies]
macos = [
    "pyobjc-framework-ApplicationServices>=10.0",
    "pyobjc-framework-Quartz>=10.0",
    "pyobjc-framework-Cocoa>=10.0",
    "pyobjc-framework-Vision>=10.0",
]
```

**Step 3: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv pip install -e ".[macos]" && uv run python -c "from agent_eyes.ocr import get_ocr_engine; e = get_ocr_engine(); print(f'Engine: {e.__class__.__name__}' if e else 'No engine')"`

Expected: `Engine: MacOSOCR`

**Step 4: Commit**

```bash
git add src/agent_eyes/ocr.py pyproject.toml
git commit -m "feat: cross-platform OCR engine abstraction

- macOS: Apple Vision VNRecognizeTextRequest (zero extra deps)
- Windows: Windows.Media.Ocr stub (winrt)
- Linux: pytesseract + Tesseract
- Returns OCRHint objects with screen-space bounding boxes
- Handles Retina scaling (pixel→point conversion)"
```

---

### Task 7: `eyes_get_ocr_hints` tool and coordinate click

**Why:** Agent needs a way to get visual text hints when AX tree is insufficient, and click by coordinates from OCR results.

**Files:**
- Modify: `src/agent_eyes/server.py` (add tool + handler + dispatch + coordinate click)

**Step 1: Add `eyes_get_ocr_hints` tool definition**

Add to the `TOOLS` list in `server.py` (after `eyes_fill_form`):

```python
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
```

**Step 2: Add coordinate click parameters to `eyes_click`**

Update the `eyes_click` tool definition to accept optional `x`, `y`, `pid`:

```python
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
```

**Step 3: Implement `_handle_get_ocr_hints`**

Add the handler function in `server.py`:

```python
def _handle_get_ocr_hints(args: dict) -> str:
    pid = args.get("pid")
    if pid is None:
        return "ERROR: pid is required."

    from .screenshot import capture_window
    from .ocr import get_ocr_engine

    engine = get_ocr_engine()
    if engine is None:
        platform = sys.platform
        if platform == "darwin":
            msg = "Install: pip install 'agent-eyes[macos]'"
        elif platform == "win32":
            msg = "Install: pip install winrt-Windows.Media.Ocr"
        else:
            msg = "Install: pip install pytesseract (+ apt install tesseract-ocr)"
        return f"ERROR: No OCR engine available. {msg}"

    capture = capture_window(pid)
    if capture is None:
        if sys.platform == "darwin":
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

    # Format hints for the agent
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
```

**Step 4: Update `_handle_click` for coordinate clicks**

Add coordinate click path at the beginning of `_handle_click`:

```python
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

    # Element ID-based click (existing logic below)
    if element_id is None:
        return "ERROR: id is required (or provide x, y coordinates)."
    # ... rest of existing _handle_click code ...
```

**Step 5: Add dispatch entry for `eyes_get_ocr_hints`**

Add to `_dispatch` in `server.py` (before the `else` clause):

```python
elif name == "eyes_get_ocr_hints":
    return _handle_get_ocr_hints(args)
```

**Step 6: Verify imports OK**

Run: `cd /Users/mekari/mcp-servers/mcp-eyes && uv run python -c "from agent_eyes.server import _dispatch; print('OK')"`

Expected: `OK`

**Step 7: Manual test — OCR on Pritunl**

Reconnect MCP, then: `eyes_get_ocr_hints(pid=1204)` — should return text like "Connect", "Settings", "Disconnected" etc. with coordinates.

Then: `eyes_click(x=<connect_x>, y=<connect_y>, pid=1204)` — should click the Connect button.

**Step 8: Commit**

```bash
git add src/agent_eyes/server.py
git commit -m "feat: eyes_get_ocr_hints tool + coordinate click support

- New tool: eyes_get_ocr_hints(pid) for visual text detection
- Returns OCRHint objects with screen-space coordinates
- eyes_click now accepts (x, y, pid) for coordinate-based clicks
- Agent explicitly requests OCR when AX tree is insufficient"
```

---

## Verification Checklist

After all tasks, verify the full flow:

1. **Native app (Finder)**: `eyes_get_tree` works as before, fast, no regression
2. **Electron app (Pritunl)**: `eyes_get_tree` now shows buttons (Connect, Settings, Import)
3. **Large Electron app (VS Code)**: `eyes_get_tree` shows toolbar, editor, sidebar elements
4. **OCR fallback**: `eyes_get_ocr_hints` returns text positions with coordinates
5. **Coordinate click**: `eyes_click(x=..., y=..., pid=...)` clicks at screen position
6. **Chrome browser**: No regression — CDP path still works via `eyes_get_web_tree`

Run: `uv pip install -e ".[macos]"` to install with Vision framework dependency.
