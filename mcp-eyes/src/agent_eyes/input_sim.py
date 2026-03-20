"""Cross-platform human-like input simulation for agent-eyes.

Simulates REAL keyboard and mouse events at the OS level, triggering
all event listeners (onInput, onChange, keyDown, keyUp) just like
physical hardware input. This is critical for apps that use event
listeners to validate input (e.g., Jamf, web apps in Chrome).

Platform support:
  - macOS:   CGEvent API via PyObjC (HID-level, indistinguishable from hardware)
  - Linux:   xdotool (X11) / ydotool (Wayland) via subprocess
  - Windows: SendInput via ctypes (kernel input stream)

Usage:
    from agent_eyes.input_sim import get_input_backend
    backend = get_input_backend()
    backend.activate_window(pid)
    backend.click(x, y)
    backend.type_text("hello")
    backend.select_all()
    backend.press_key("return")
"""
from __future__ import annotations

import abc
import os
import sys
import time
import logging
import subprocess
import random
from typing import Optional

logger = logging.getLogger("agent-eyes")


class InputBackend(abc.ABC):
    """Abstract input simulation backend."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this backend can work on the current system."""
        ...

    @abc.abstractmethod
    def type_text(self, text: str, delay: float = 0.02, human_like: bool = True) -> bool:
        """Type text character by character, firing real key events."""
        ...

    @abc.abstractmethod
    def press_key(self, key: str) -> bool:
        """Press a special key (return, tab, delete, escape, etc.)."""
        ...

    @abc.abstractmethod
    def hotkey(self, *keys: str) -> bool:
        """Press a key combination (e.g., hotkey('command', 'a') for Cmd+A)."""
        ...

    @abc.abstractmethod
    def click(self, x: int, y: int, button: str = "left") -> bool:
        """Click at absolute screen coordinates."""
        ...

    @abc.abstractmethod
    def activate_window(self, pid: int) -> bool:
        """Bring a window to the foreground by PID."""
        ...

    def select_all(self) -> bool:
        """Select all text in the focused element (Cmd+A / Ctrl+A)."""
        mod = "command" if sys.platform == "darwin" else "control"
        return self.hotkey(mod, "a")

    def clear_field(self) -> bool:
        """Clear a text field like a human: select all, then delete."""
        if not self.select_all():
            return False
        time.sleep(0.05)
        return self.press_key("delete")

    def clear_and_type(self, text: str, delay: float = 0.02) -> bool:
        """Clear a field and type new text — the full human-like sequence."""
        if not self.clear_field():
            return False
        time.sleep(0.05)
        return self.type_text(text, delay=delay)

    def click_and_type(
        self, x: int, y: int, text: str, clear: bool = True, delay: float = 0.02
    ) -> bool:
        """Click an element, optionally clear it, and type text."""
        if not self.click(x, y):
            return False
        time.sleep(0.08)
        if clear:
            return self.clear_and_type(text, delay=delay)
        return self.type_text(text, delay=delay)


# ── macOS: CGEvent via PyObjC ──────────────────────────────────────

class MacOSInputBackend(InputBackend):
    """macOS input simulation using Quartz CGEvent API."""

    # Carbon virtual key codes
    _KEY_CODES = {
        "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04,
        "g": 0x05, "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09,
        "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F,
        "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14,
        "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A,
        "8": 0x1C, "0": 0x1D, "o": 0x1F, "u": 0x20, "i": 0x22,
        "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D,
        "m": 0x2E,
        "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31,
        "delete": 0x33, "backspace": 0x33, "escape": 0x35, "esc": 0x35,
        "command": 0x37, "shift": 0x38, "option": 0x3A, "alt": 0x3A,
        "control": 0x3B, "ctrl": 0x3B,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
        "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
        "forward_delete": 0x75,
        "home": 0x73, "end": 0x77, "page_up": 0x74, "page_down": 0x79,
        "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    }

    def __init__(self):
        self._quartz = None

    def _load(self):
        if self._quartz is not None:
            return
        import Quartz
        self._quartz = Quartz

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Quartz  # noqa: F401
            return True
        except ImportError:
            return False

    def type_text(self, text: str, delay: float = 0.02, human_like: bool = True) -> bool:
        self._load()
        Q = self._quartz
        source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        try:
            for char in text:
                # Key down with unicode character
                event = Q.CGEventCreateKeyboardEvent(source, 0, True)
                Q.CGEventKeyboardSetUnicodeString(event, 1, char)
                Q.CGEventPost(Q.kCGSessionEventTap, event)

                # Key up
                event = Q.CGEventCreateKeyboardEvent(source, 0, False)
                Q.CGEventKeyboardSetUnicodeString(event, 1, char)
                Q.CGEventPost(Q.kCGSessionEventTap, event)

                # Human-like variable delay
                if human_like:
                    d = delay * random.uniform(0.7, 1.3)
                    if char in " .,;:!?\n":
                        d *= random.uniform(1.3, 2.0)
                    time.sleep(d)
                else:
                    time.sleep(delay)
            return True
        except Exception as e:
            logger.error("macOS type_text failed: %s", e)
            return False

    def press_key(self, key: str) -> bool:
        self._load()
        Q = self._quartz
        key_code = self._KEY_CODES.get(key.lower())
        if key_code is None:
            logger.error("Unknown key: %s", key)
            return False
        try:
            event = Q.CGEventCreateKeyboardEvent(None, key_code, True)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            time.sleep(0.01)
            event = Q.CGEventCreateKeyboardEvent(None, key_code, False)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error("macOS press_key failed: %s", e)
            return False

    def hotkey(self, *keys: str) -> bool:
        self._load()
        Q = self._quartz
        modifier_flags = {
            "command": Q.kCGEventFlagMaskCommand,
            "shift": Q.kCGEventFlagMaskShift,
            "option": Q.kCGEventFlagMaskAlternate, "alt": Q.kCGEventFlagMaskAlternate,
            "control": Q.kCGEventFlagMaskControl, "ctrl": Q.kCGEventFlagMaskControl,
        }
        try:
            # Separate modifiers from the final key
            mods = keys[:-1]
            final_key = keys[-1]
            flags = 0
            for m in mods:
                flag = modifier_flags.get(m.lower())
                if flag:
                    flags |= flag

            key_code = self._KEY_CODES.get(final_key.lower())
            if key_code is None:
                return False

            # Key down with modifiers
            event = Q.CGEventCreateKeyboardEvent(None, key_code, True)
            if flags:
                Q.CGEventSetFlags(event, flags)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            time.sleep(0.01)

            # Key up
            event = Q.CGEventCreateKeyboardEvent(None, key_code, False)
            if flags:
                Q.CGEventSetFlags(event, flags)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error("macOS hotkey failed: %s", e)
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        self._load()
        Q = self._quartz
        try:
            point = (float(x), float(y))
            if button == "right":
                down_type, up_type, btn = (
                    Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp,
                    Q.kCGMouseButtonRight,
                )
            else:
                down_type, up_type, btn = (
                    Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
                    Q.kCGMouseButtonLeft,
                )

            event = Q.CGEventCreateMouseEvent(None, down_type, point, btn)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            time.sleep(0.01)
            event = Q.CGEventCreateMouseEvent(None, up_type, point, btn)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error("macOS click failed: %s", e)
            return False

    def activate_window(self, pid: int) -> bool:
        """Bring an app to front via NSRunningApplication (Cocoa API).

        More reliable than AppleScript — works for apps that don't respond
        to System Events (like Jamf Self Service+).
        """
        try:
            from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app:
                app.activateWithOptionSet_(NSApplicationActivateIgnoringOtherApps)
                time.sleep(0.15)
                return True
            # Fallback to System Events if NSRunningApplication fails
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {pid}) to true'],
                capture_output=True, timeout=3,
            )
            time.sleep(0.15)
            return True
        except Exception as e:
            logger.error("macOS activate_window failed: %s", e)
            return False


# ── Linux: xdotool / ydotool ──────────────────────────────────────

class LinuxInputBackend(InputBackend):
    """Linux input simulation using xdotool (X11) or ydotool (Wayland)."""

    def __init__(self):
        self._display_server: Optional[str] = None
        self._tool: Optional[str] = None

    def _detect(self):
        if self._display_server is not None:
            return

        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            self._display_server = "wayland"
        else:
            self._display_server = "x11"

        # Check available tools
        if self._display_server == "x11":
            self._tool = "xdotool" if self._cmd_exists("xdotool") else None
        else:
            # Wayland: prefer ydotool, then wtype, then xdotool via XWayland
            if self._cmd_exists("ydotool"):
                self._tool = "ydotool"
            elif self._cmd_exists("wtype"):
                self._tool = "wtype"
            elif self._cmd_exists("xdotool"):
                self._tool = "xdotool"  # XWayland fallback

    @staticmethod
    def _cmd_exists(cmd: str) -> bool:
        try:
            result = subprocess.run(
                ["which", cmd], capture_output=True, timeout=2,
            )
            return result.returncode == 0
        except Exception:
            return False

    def is_available(self) -> bool:
        if sys.platform != "linux":
            return False
        self._detect()
        return self._tool is not None

    def type_text(self, text: str, delay: float = 0.02, human_like: bool = True) -> bool:
        self._detect()
        delay_ms = int(delay * 1000)
        try:
            if self._tool == "xdotool":
                subprocess.run(
                    ["xdotool", "type", "--delay", str(delay_ms), text],
                    capture_output=True, timeout=30,
                )
            elif self._tool == "ydotool":
                subprocess.run(
                    ["ydotool", "type", "--key-delay", str(delay_ms), text],
                    capture_output=True, timeout=30,
                )
            elif self._tool == "wtype":
                subprocess.run(
                    ["wtype", "-d", str(delay_ms), text],
                    capture_output=True, timeout=30,
                )
            else:
                return False
            return True
        except Exception as e:
            logger.error("Linux type_text failed: %s", e)
            return False

    # xdotool key name mapping
    _XDOTOOL_KEYS = {
        "return": "Return", "enter": "Return", "tab": "Tab",
        "delete": "BackSpace", "backspace": "BackSpace",
        "forward_delete": "Delete", "escape": "Escape", "esc": "Escape",
        "space": "space", "home": "Home", "end": "End",
        "page_up": "Page_Up", "page_down": "Page_Down",
        "left": "Left", "right": "Right", "up": "Up", "down": "Down",
        "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
        "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
        "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    }

    # ydotool uses Linux evdev key codes
    _YDOTOOL_KEYS = {
        "return": 28, "enter": 28, "tab": 15,
        "delete": 14, "backspace": 14,
        "forward_delete": 111, "escape": 1, "esc": 1,
        "space": 57, "home": 102, "end": 107,
        "page_up": 104, "page_down": 109,
        "left": 105, "right": 106, "up": 103, "down": 108,
        "a": 30, "control": 29, "ctrl": 29, "shift": 42, "alt": 56,
    }

    def press_key(self, key: str) -> bool:
        self._detect()
        try:
            if self._tool == "xdotool":
                xkey = self._XDOTOOL_KEYS.get(key.lower(), key)
                subprocess.run(
                    ["xdotool", "key", xkey],
                    capture_output=True, timeout=5,
                )
            elif self._tool == "ydotool":
                code = self._YDOTOOL_KEYS.get(key.lower())
                if code is None:
                    return False
                subprocess.run(
                    ["ydotool", "key", f"{code}:1", f"{code}:0"],
                    capture_output=True, timeout=5,
                )
            elif self._tool == "wtype":
                xkey = self._XDOTOOL_KEYS.get(key.lower(), key)
                subprocess.run(
                    ["wtype", "-k", xkey],
                    capture_output=True, timeout=5,
                )
            else:
                return False
            return True
        except Exception as e:
            logger.error("Linux press_key failed: %s", e)
            return False

    def hotkey(self, *keys: str) -> bool:
        self._detect()
        try:
            if self._tool == "xdotool":
                combo = "+".join(
                    self._XDOTOOL_KEYS.get(k.lower(), k) for k in keys
                )
                subprocess.run(
                    ["xdotool", "key", combo],
                    capture_output=True, timeout=5,
                )
            elif self._tool == "ydotool":
                # Press all modifiers, then key, then release in reverse
                events = []
                for k in keys:
                    code = self._YDOTOOL_KEYS.get(k.lower())
                    if code is None:
                        return False
                    events.append(f"{code}:1")
                for k in reversed(keys):
                    code = self._YDOTOOL_KEYS.get(k.lower())
                    events.append(f"{code}:0")
                subprocess.run(
                    ["ydotool", "key"] + events,
                    capture_output=True, timeout=5,
                )
            elif self._tool == "wtype":
                # wtype: -M mod -k key -m mod
                mods = keys[:-1]
                final = keys[-1]
                cmd = ["wtype"]
                for m in mods:
                    cmd.extend(["-M", m.lower()])
                cmd.extend(["-k", self._XDOTOOL_KEYS.get(final.lower(), final)])
                for m in reversed(mods):
                    cmd.extend(["-m", m.lower()])
                subprocess.run(cmd, capture_output=True, timeout=5)
            else:
                return False
            return True
        except Exception as e:
            logger.error("Linux hotkey failed: %s", e)
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        self._detect()
        btn = "3" if button == "right" else "1"
        try:
            if self._tool in ("xdotool",):
                subprocess.run(
                    ["xdotool", "mousemove", str(x), str(y)],
                    capture_output=True, timeout=5,
                )
                time.sleep(0.01)
                subprocess.run(
                    ["xdotool", "click", btn],
                    capture_output=True, timeout=5,
                )
            elif self._tool == "ydotool":
                subprocess.run(
                    ["ydotool", "mousemove", "--absolute",
                     "-x", str(x), "-y", str(y)],
                    capture_output=True, timeout=5,
                )
                time.sleep(0.01)
                click_code = "0xC1" if button == "right" else "0xC0"
                subprocess.run(
                    ["ydotool", "click", click_code],
                    capture_output=True, timeout=5,
                )
            else:
                return False
            return True
        except Exception as e:
            logger.error("Linux click failed: %s", e)
            return False

    def activate_window(self, pid: int) -> bool:
        self._detect()
        if self._tool == "xdotool":
            try:
                result = subprocess.run(
                    ["xdotool", "search", "--pid", str(pid)],
                    capture_output=True, text=True, timeout=5,
                )
                window_ids = result.stdout.strip().split("\n")
                if window_ids and window_ids[0]:
                    subprocess.run(
                        ["xdotool", "windowactivate", "--sync", window_ids[0]],
                        capture_output=True, timeout=5,
                    )
                    time.sleep(0.3)
                    return True
            except Exception:
                pass
        return False


# ── Windows: SendInput via ctypes ──────────────────────────────────

class WindowsInputBackend(InputBackend):
    """Windows input simulation using SendInput API via ctypes."""

    # Virtual key codes
    _VK = {
        "return": 0x0D, "enter": 0x0D, "tab": 0x09,
        "delete": 0x08, "backspace": 0x08,
        "forward_delete": 0x2E, "escape": 0x1B, "esc": 0x1B,
        "space": 0x20, "home": 0x24, "end": 0x23,
        "page_up": 0x21, "page_down": 0x22,
        "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
        "control": 0x11, "ctrl": 0x11,
        "shift": 0x10, "alt": 0x12, "command": 0x5B,  # Win key
        "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
        "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
        "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
        "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
        "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
        "z": 0x5A,
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
        "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    }

    # These keys need KEYEVENTF_EXTENDEDKEY
    _EXTENDED = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x27, 0x26, 0x28}

    def __init__(self):
        self._user32 = None
        self._INPUT = None
        self._KEYBDINPUT = None
        self._MOUSEINPUT = None

    def _load(self):
        if self._user32 is not None:
            return
        import ctypes
        from ctypes import wintypes

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _INPUT(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
            _anonymous_ = ("_input",)
            _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT)]

        self._INPUT = INPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._MOUSEINPUT = MOUSEINPUT

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes  # noqa: F401
            return True
        except ImportError:
            return False

    def _send_input(self, inp):
        import ctypes
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def type_text(self, text: str, delay: float = 0.02, human_like: bool = True) -> bool:
        self._load()
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        try:
            for char in text:
                scan = ord(char)
                # Key down
                inp = self._INPUT(
                    type=1,
                    _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                        wVk=0, wScan=scan, dwFlags=KEYEVENTF_UNICODE,
                    )),
                )
                self._send_input(inp)
                # Key up
                inp = self._INPUT(
                    type=1,
                    _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                        wVk=0, wScan=scan,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                    )),
                )
                self._send_input(inp)

                if human_like:
                    d = delay * random.uniform(0.7, 1.3)
                    if char in " .,;:!?\n":
                        d *= random.uniform(1.3, 2.0)
                    time.sleep(d)
                else:
                    time.sleep(delay)
            return True
        except Exception as e:
            logger.error("Windows type_text failed: %s", e)
            return False

    def _tap_vk(self, vk: int):
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_EXTENDEDKEY = 0x0001
        flags = KEYEVENTF_EXTENDEDKEY if vk in self._EXTENDED else 0

        inp = self._INPUT(
            type=1,
            _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                wVk=vk, dwFlags=flags,
            )),
        )
        self._send_input(inp)
        time.sleep(0.01)
        inp = self._INPUT(
            type=1,
            _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                wVk=vk, dwFlags=flags | KEYEVENTF_KEYUP,
            )),
        )
        self._send_input(inp)

    def _press_vk(self, vk: int):
        flags = 0x0001 if vk in self._EXTENDED else 0
        inp = self._INPUT(
            type=1,
            _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                wVk=vk, dwFlags=flags,
            )),
        )
        self._send_input(inp)

    def _release_vk(self, vk: int):
        flags = (0x0001 if vk in self._EXTENDED else 0) | 0x0002
        inp = self._INPUT(
            type=1,
            _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                wVk=vk, dwFlags=flags,
            )),
        )
        self._send_input(inp)

    def press_key(self, key: str) -> bool:
        self._load()
        vk = self._VK.get(key.lower())
        if vk is None:
            return False
        try:
            self._tap_vk(vk)
            return True
        except Exception as e:
            logger.error("Windows press_key failed: %s", e)
            return False

    def hotkey(self, *keys: str) -> bool:
        self._load()
        vks = [self._VK.get(k.lower()) for k in keys]
        if any(v is None for v in vks):
            return False
        try:
            # Press modifiers, tap final key, release modifiers
            for vk in vks[:-1]:
                self._press_vk(vk)
                time.sleep(0.01)
            self._tap_vk(vks[-1])
            for vk in reversed(vks[:-1]):
                self._release_vk(vk)
            return True
        except Exception as e:
            logger.error("Windows hotkey failed: %s", e)
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        self._load()
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_ABSOLUTE = 0x8000
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010

        try:
            screen_w = self._user32.GetSystemMetrics(0)
            screen_h = self._user32.GetSystemMetrics(1)
            nx = int(x * 65535 / screen_w)
            ny = int(y * 65535 / screen_h)

            down_flag = MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN
            up_flag = MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP

            # Move
            inp = self._INPUT(
                type=0,
                _input=self._INPUT._INPUT(mi=self._MOUSEINPUT(
                    dx=nx, dy=ny,
                    dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                )),
            )
            self._send_input(inp)
            time.sleep(0.01)

            # Down
            inp = self._INPUT(
                type=0,
                _input=self._INPUT._INPUT(mi=self._MOUSEINPUT(
                    dwFlags=down_flag,
                )),
            )
            self._send_input(inp)
            time.sleep(0.01)

            # Up
            inp = self._INPUT(
                type=0,
                _input=self._INPUT._INPUT(mi=self._MOUSEINPUT(
                    dwFlags=up_flag,
                )),
            )
            self._send_input(inp)
            return True
        except Exception as e:
            logger.error("Windows click failed: %s", e)
            return False

    def activate_window(self, pid: int) -> bool:
        self._load()
        try:
            # Find window by PID
            import ctypes
            from ctypes import wintypes

            EnumWindows = self._user32.EnumWindows
            GetWindowThreadProcessId = self._user32.GetWindowThreadProcessId
            IsWindowVisible = self._user32.IsWindowVisible
            SetForegroundWindow = self._user32.SetForegroundWindow

            target_hwnd = None

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def callback(hwnd, _):
                nonlocal target_hwnd
                if IsWindowVisible(hwnd):
                    proc_id = wintypes.DWORD()
                    GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
                    if proc_id.value == pid:
                        target_hwnd = hwnd
                        return False  # Stop enumeration
                return True

            EnumWindows(callback, 0)
            if target_hwnd:
                SetForegroundWindow(target_hwnd)
                time.sleep(0.3)
                return True
            return False
        except Exception as e:
            logger.error("Windows activate_window failed: %s", e)
            return False


# ── Fallback: AppleScript (macOS only, simpler) ───────────────────

class AppleScriptInputBackend(InputBackend):
    """Fallback macOS input using osascript System Events (slower but no PyObjC)."""

    def is_available(self) -> bool:
        return sys.platform == "darwin"

    def type_text(self, text: str, delay: float = 0.02, human_like: bool = True) -> bool:
        try:
            # Use JXA for reliable Unicode handling
            import json
            script = f'''
            var se = Application("System Events");
            var text = {json.dumps(text)};
            for (var i = 0; i < text.length; i++) {{
                se.keystroke(text[i]);
                delay({delay});
            }}
            '''
            subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script],
                capture_output=True, timeout=max(30, len(text) * delay * 2),
            )
            return True
        except Exception as e:
            logger.error("AppleScript type_text failed: %s", e)
            return False

    def press_key(self, key: str) -> bool:
        # Map to AppleScript key codes
        key_codes = {
            "return": 36, "enter": 36, "tab": 48, "delete": 51,
            "backspace": 51, "escape": 53, "space": 49,
            "left": 123, "right": 124, "down": 125, "up": 126,
        }
        code = key_codes.get(key.lower())
        if code is None:
            return False
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to key code {code}'],
                capture_output=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def hotkey(self, *keys: str) -> bool:
        modifiers = {
            "command": "command down", "shift": "shift down",
            "option": "option down", "alt": "option down",
            "control": "control down", "ctrl": "control down",
        }
        mods = [modifiers.get(k.lower()) for k in keys[:-1] if k.lower() in modifiers]
        final_key = keys[-1]
        if not mods:
            return False
        mod_str = ", ".join(mods)
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to keystroke "{final_key}" '
                 f'using {{{mod_str}}}'],
                capture_output=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        # AppleScript can't easily do coordinate clicks; not supported
        return False

    def activate_window(self, pid: int) -> bool:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {pid}) to true'],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return True
        except Exception:
            return False


# ── Factory ────────────────────────────────────────────────────────

_backend_cache: Optional[InputBackend] = None


def get_input_backend() -> InputBackend:
    """Get the best available input simulation backend for the current platform."""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache

    if sys.platform == "darwin":
        backend = MacOSInputBackend()
        if backend.is_available():
            _backend_cache = backend
            logger.info("Input backend: macOS CGEvent")
            return backend
        # Fallback to AppleScript
        backend = AppleScriptInputBackend()
        _backend_cache = backend
        logger.info("Input backend: macOS AppleScript (fallback)")
        return backend

    elif sys.platform == "win32":
        backend = WindowsInputBackend()
        if backend.is_available():
            _backend_cache = backend
            logger.info("Input backend: Windows SendInput")
            return backend

    elif sys.platform == "linux":
        backend = LinuxInputBackend()
        if backend.is_available():
            _backend_cache = backend
            logger.info("Input backend: Linux %s", backend._tool)
            return backend

    # Ultimate fallback — return a no-op that always fails
    logger.warning("No input backend available")
    return _NoOpBackend()


class _NoOpBackend(InputBackend):
    def is_available(self) -> bool:
        return False
    def type_text(self, text, delay=0.02, human_like=True) -> bool:
        return False
    def press_key(self, key) -> bool:
        return False
    def hotkey(self, *keys) -> bool:
        return False
    def click(self, x, y, button="left") -> bool:
        return False
    def activate_window(self, pid) -> bool:
        return False
