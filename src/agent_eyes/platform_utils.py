"""Cross-platform utilities for agent-eyes.

Provides platform-agnostic browser detection, optional CDP discovery,
and URL opening. Works on macOS, Linux, and Windows.
"""
from __future__ import annotations

import functools
import logging
import os
import sys
import subprocess
from pathlib import Path


logger = logging.getLogger("agent-eyes")


# ── Browser detection ──────────────────────────────────────────────

# Known browser process names (lowercase). This is used only to tell native
# accessibility adapters to retain web-document content; it does not select a
# browser protocol or debugging endpoint.
_BROWSER_PROCESSES = (
    "google chrome", "google-chrome", "chrome", "chromium", "chromium-browser",
    "brave", "brave browser", "brave-browser", "microsoft edge", "microsoft-edge", "msedge", "arc",
    "vivaldi", "opera", "opera gx",
    "safari", "safari technology preview", "firefox", "firefox developer edition",
    "librewolf", "waterfox", "floorp", "zen", "zen browser", "zen-browser",
    "duckduckgo", "orion", "dia",
)


@functools.lru_cache(maxsize=64)
def get_process_name(pid: int) -> str:
    """Get the process name for a PID. Cross-platform. Cached per PID."""
    if sys.platform == "win32":
        return _get_process_name_windows(pid)
    else:
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
    """Check if a PID belongs to a known browser family. Cross-platform."""
    executable = Path(get_process_name(pid)).name.casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable in _BROWSER_PROCESSES


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
    detection internally and respects the user's selected default browser.
    Returns (success: bool, message: str).
    """
    import webbrowser

    try:
        if webbrowser.open_new_tab(url):
            return True, "Opened the requested URL in the default browser."

        return False, "The operating system did not provide a default browser."
    except Exception as exc:
        logger.debug("Default-browser open failed (%s)", type(exc).__name__)
        return False, "The operating system could not open the requested URL."
