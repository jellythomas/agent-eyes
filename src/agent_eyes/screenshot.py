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
