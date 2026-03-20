"""Cross-platform utilities for agent-eyes.

Provides platform-agnostic browser detection, CDP auto-discovery,
and Chrome launch instructions. Works on macOS, Linux, and Windows.
"""
from __future__ import annotations

import os
import socket
import sys
import subprocess
import time
from pathlib import Path


# ── Browser detection ──────────────────────────────────────────────

# Known Chromium-based browser process names (lowercase)
_CHROMIUM_BROWSERS = (
    "google chrome", "google-chrome", "chrome", "chromium", "chromium-browser",
    "brave", "brave browser", "microsoft edge", "msedge", "arc",
    "vivaldi", "opera", "opera gx",
)


def get_process_name(pid: int) -> str:
    """Get the process name for a PID. Cross-platform."""
    if sys.platform == "win32":
        return _get_process_name_windows(pid)
    else:
        # macOS and Linux both support ps
        return _get_process_name_unix(pid)


def _get_process_name_unix(pid: int) -> str:
    """Get process name via ps (macOS/Linux)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _get_process_name_windows(pid: int) -> str:
    """Get process name via tasklist (Windows)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output: "chrome.exe","12345","Console","1","123,456 K"
            parts = result.stdout.strip().split(",")
            if parts:
                return parts[0].strip('"').replace(".exe", "")
        return ""
    except Exception:
        return ""


def is_browser_pid(pid: int) -> bool:
    """Check if a PID belongs to a Chromium-based browser. Cross-platform."""
    name = get_process_name(pid).lower()
    return any(b in name for b in _CHROMIUM_BROWSERS)


# ── CDP auto-discovery ─────────────────────────────────────────────

def get_chrome_profile_dirs() -> list[Path]:
    """Get Chrome/Chromium profile directories for the current platform."""
    home = Path.home()
    dirs: list[Path] = []

    if sys.platform == "darwin":
        dirs = [
            home / "Library" / "Application Support" / "Google" / "Chrome",
            home / "Library" / "Application Support" / "Chromium",
            home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
            home / "Library" / "Application Support" / "Microsoft Edge",
            home / "Library" / "Application Support" / "Arc" / "User Data",
            home / "Library" / "Application Support" / "Vivaldi",
        ]
    elif sys.platform == "linux":
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        dirs = [
            config / "google-chrome",
            config / "chromium",
            config / "BraveSoftware" / "Brave-Browser",
            config / "microsoft-edge",
            config / "vivaldi",
        ]
    elif sys.platform == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs = [
            local_app / "Google" / "Chrome" / "User Data",
            local_app / "Chromium" / "User Data",
            local_app / "BraveSoftware" / "Brave-Browser" / "User Data",
            local_app / "Microsoft" / "Edge" / "User Data",
            local_app / "Vivaldi" / "User Data",
        ]

    return [d for d in dirs if d.exists()]


def discover_cdp_port() -> int | None:
    """Auto-discover Chrome's CDP port from DevToolsActivePort file.

    Chrome writes this file when started with --remote-debugging-port=0
    or any port. It contains:
      Line 1: port number
      Line 2: WebSocket path

    Returns the port number or None if not found.
    """
    for profile_dir in get_chrome_profile_dirs():
        port_file = profile_dir / "DevToolsActivePort"
        if port_file.exists():
            try:
                content = port_file.read_text().strip()
                lines = content.split("\n")
                if lines:
                    port = int(lines[0].strip())
                    if 1024 <= port <= 65535:
                        return port
            except (ValueError, OSError):
                continue
    return None


# ── Chrome launch instructions ─────────────────────────────────────

def get_chrome_launch_cmd() -> str:
    """Get the platform-appropriate Chrome launch command with CDP enabled."""
    if sys.platform == "darwin":
        return "open -a 'Google Chrome' --args --remote-debugging-port=9222"
    elif sys.platform == "win32":
        return 'start chrome --remote-debugging-port=9222'
    else:
        # Linux — try common binary names
        for binary in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            try:
                result = subprocess.run(
                    ["which", binary], capture_output=True, timeout=2,
                )
                if result.returncode == 0:
                    return f"{binary} --remote-debugging-port=9222"
            except Exception:
                continue
        return "google-chrome --remote-debugging-port=9222"


def get_chrome_binary() -> str | None:
    """Find the Chrome/Chromium binary path. Returns None if not found."""
    if sys.platform == "darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    elif sys.platform == "win32":
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for pf in program_files:
            if not pf:
                continue
            for subpath in (
                r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ):
                full = os.path.join(pf, subpath)
                if os.path.exists(full):
                    return full
    else:
        # Linux
        for binary in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            try:
                result = subprocess.run(
                    ["which", binary], capture_output=True, text=True, timeout=2,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except Exception:
                continue
    return None


# ── Cross-platform browser URL opening ────────────────────────────

def open_url_in_browser(url: str) -> tuple[bool, str]:
    """Open a URL in the system browser. Works on macOS, Windows, Linux.

    Uses Python's stdlib ``webbrowser`` module which handles all platform
    detection internally. If Chrome is already running, it opens a new tab.
    Returns (success: bool, message: str).
    """
    import webbrowser

    try:
        # Try Chrome/Chromium first via registered browser names
        for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
            try:
                browser = webbrowser.get(name)
                if browser.open_new_tab(url):
                    return True, f"Opened in Chrome: {url}"
            except webbrowser.Error:
                continue

        # Fallback to system default browser
        if webbrowser.open_new_tab(url):
            return True, f"Opened in default browser: {url}"

        return False, "No browser found. Install Google Chrome or Chromium."
    except Exception as e:
        return False, f"Failed to open URL: {e}"
