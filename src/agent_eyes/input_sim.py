"""Cross-platform input simulation for agent-eyes.

Simulates REAL keyboard and mouse events at the OS level, triggering
application input handling. Text uses each platform's bounded native bulk path
by default; callers can explicitly request slower per-character timing with
``human_like=True``.

Platform support:
  - macOS:   CGEvent API via PyObjC (HID-level, indistinguishable from hardware)
  - Linux:   python-xlib XTest extension (X11, no subprocess)
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
import sys
import time
import logging
import subprocess
import random
from typing import Optional

logger = logging.getLogger("agent-eyes")

_MACOS_UNICODE_EVENT_UNIT_LIMIT = 20


def _utf16_code_units(text: str) -> tuple[int, ...]:
    """Return the UTF-16 code units required by native keyboard APIs."""
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return tuple(
        encoded[index] | (encoded[index + 1] << 8)
        for index in range(0, len(encoded), 2)
    )


def _chunks_by_utf16_units(text: str, limit: int) -> tuple[str, ...]:
    """Split text without breaking a supplementary Unicode character."""
    if limit < 1:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in text:
        character_units = len(_utf16_code_units(character))
        if current and current_units + character_units > limit:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current:
        chunks.append("".join(current))
    return tuple(chunks)


class InputBackend(abc.ABC):
    """Abstract input simulation backend."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this backend can work on the current system."""
        ...

    @abc.abstractmethod
    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        """Type text, using fast native batching unless human-like mode is explicit."""
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
    def scroll(self, x: int, y: int, delta_x: int = 0, delta_y: int = -3) -> bool:
        """Scroll at absolute screen coordinates. delta_y negative = down, positive = up."""
        ...

    @abc.abstractmethod
    def activate_window(self, pid: int) -> bool:
        """Bring a window to the foreground by PID."""
        ...

    def double_click(self, x: int, y: int) -> bool:
        """Double-click at absolute screen coordinates."""
        return False

    def move_mouse(self, x: int, y: int) -> bool:
        """Move mouse to absolute screen coordinates without clicking."""
        return False

    def paste_text(self, text: str) -> bool:
        """Paste text via clipboard. Override per platform."""
        return False

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> bool:
        """Drag from one screen position to another."""
        return False

    def select_all(self) -> bool:
        """Select all text in the focused element (Cmd+A / Ctrl+A)."""
        mod = "command" if sys.platform == "darwin" else "control"
        return self.hotkey(mod, "a")

    def is_frontmost(self, pid: int) -> bool:
        """Check if a given PID is the frontmost application."""
        return False  # Override per platform

    def clear_field(self) -> bool:
        """Clear a text field like a human: select all, then delete."""
        if not self.select_all():
            return False
        return self.press_key("delete")

    def clear_and_type(self, text: str, delay: float = 0.02) -> bool:
        """Clear a field and type new text with the default fast text path."""
        if not self.clear_field():
            return False
        return self.type_text(text, delay=delay)

    def click_and_type(
        self, x: int, y: int, text: str, clear: bool = True, delay: float = 0.02
    ) -> bool:
        """Click an element, optionally clear it, and type text."""
        if not self.click(x, y):
            return False
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
            import Quartz

            preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)
            return bool(preflight()) if callable(preflight) else True
        except ImportError:
            return False
        except Exception:
            return False

    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        self._load()
        Q = self._quartz
        try:
            if not text:
                return True
            source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
            chunks = (
                tuple(text)
                if human_like
                else _chunks_by_utf16_units(
                    text,
                    _MACOS_UNICODE_EVENT_UNIT_LIMIT,
                )
            )
            for chunk in chunks:
                unit_count = len(_utf16_code_units(chunk))
                for key_down in (True, False):
                    event = Q.CGEventCreateKeyboardEvent(source, 0, key_down)
                    Q.CGEventKeyboardSetUnicodeString(event, unit_count, chunk)
                    Q.CGEventPost(Q.kCGSessionEventTap, event)

                if human_like:
                    d = delay * random.uniform(0.7, 1.3)
                    if chunk in " .,;:!?\n":
                        d *= random.uniform(1.3, 2.0)
                    time.sleep(max(0, d))
            return True
        except Exception:
            logger.error("macOS type_text failed")
            return False

    def press_key(self, key: str) -> bool:
        self._load()
        Q = self._quartz
        key_code = self._KEY_CODES.get(key.lower())
        if key_code is None:
            logger.error("Unknown key (key_length=%d)", len(key))
            return False
        try:
            # Use kCGSessionEventTap (not kCGHIDEventTap) to target the
            # frontmost app's session instead of broadcasting system-wide.
            # kCGHIDEventTap posts at HID level where keys like Escape get
            # intercepted by terminal emulators / Claude Code before reaching
            # the target app.
            source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
            event = Q.CGEventCreateKeyboardEvent(source, key_code, True)
            Q.CGEventPost(Q.kCGSessionEventTap, event)
            time.sleep(0.01)
            event = Q.CGEventCreateKeyboardEvent(source, key_code, False)
            Q.CGEventPost(Q.kCGSessionEventTap, event)
            return True
        except Exception as e:
            logger.error(
                "macOS press_key failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def hotkey(self, *keys: str) -> bool:
        if len(keys) < 2:
            return False
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
                if flag is None:
                    return False
                flags |= flag

            key_code = self._KEY_CODES.get(final_key.lower())
            if key_code is None:
                return False

            # Use kCGSessionEventTap to target frontmost app (see press_key)
            source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)

            # Key down with modifiers
            event = Q.CGEventCreateKeyboardEvent(source, key_code, True)
            if flags:
                Q.CGEventSetFlags(event, flags)
            Q.CGEventPost(Q.kCGSessionEventTap, event)
            time.sleep(0.01)

            # Key up
            event = Q.CGEventCreateKeyboardEvent(source, key_code, False)
            if flags:
                Q.CGEventSetFlags(event, flags)
            Q.CGEventPost(Q.kCGSessionEventTap, event)
            return True
        except Exception as e:
            logger.error(
                "macOS hotkey failed (exception_type=%s)",
                type(e).__name__,
            )
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
            logger.error(
                "macOS click failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def double_click(self, x: int, y: int) -> bool:
        self._load()
        Q = self._quartz
        try:
            point = Q.CGPointMake(x, y)
            # Click 1
            event = Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDown, point, 0)
            Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 1)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            event = Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseUp, point, 0)
            Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 1)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            time.sleep(0.02)
            # Click 2
            event = Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDown, point, 0)
            Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 2)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            event = Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseUp, point, 0)
            Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 2)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error(
                "macOS double_click failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def scroll(self, x: int, y: int, delta_x: int = 0, delta_y: int = -3) -> bool:
        self._load()
        Q = self._quartz
        try:
            # Move mouse to position first
            move = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, Q.CGPointMake(x, y), 0)
            Q.CGEventPost(Q.kCGHIDEventTap, move)
            time.sleep(0.05)
            # Create scroll event
            event = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 2, delta_y, delta_x)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error(
                "macOS scroll failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def move_mouse(self, x: int, y: int) -> bool:
        self._load()
        Q = self._quartz
        try:
            event = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, Q.CGPointMake(x, y), 0)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            return True
        except Exception as e:
            logger.error(
                "macOS move_mouse failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def paste_text(self, text: str) -> bool:
        """Paste text via clipboard (Cmd+V). 100x faster than char-by-char typing.
        Saves and restores the user's clipboard contents.
        """
        try:
            from AppKit import NSPasteboard, NSStringPboardType
            pb = NSPasteboard.generalPasteboard()

            # Save current clipboard
            old_types = pb.types()
            old_data = {}
            for t in old_types:
                d = pb.dataForType_(t)
                if d:
                    old_data[t] = d

            # Set our text
            pb.clearContents()
            pb.setString_forType_(text, NSStringPboardType)

            # Cmd+V
            time.sleep(0.05)
            self.hotkey("command", "v")
            time.sleep(0.1)

            # Restore clipboard
            pb.clearContents()
            for t, d in old_data.items():
                try:
                    pb.setData_forType_(d, t)
                except Exception:
                    pass

            return True
        except Exception as e:
            logger.error(
                "macOS paste_text failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def is_frontmost(self, pid: int) -> bool:
        """Check if a given PID is the frontmost application."""
        try:
            from AppKit import NSWorkspace
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            return front is not None and front.processIdentifier() == pid
        except Exception:
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
                return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
            # Fallback to System Events if NSRunningApplication fails
            completed = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {pid}) to true'],
                capture_output=True, timeout=3,
            )
            return completed.returncode == 0
        except Exception as e:
            logger.error(
                "macOS activate_window failed (exception_type=%s)",
                type(e).__name__,
            )
            return False


# ── Linux: python-xlib XTest ──────────────────────────────────────

class LinuxInputBackend(InputBackend):
    """Linux input simulation using python-xlib XTest extension (X11)."""

    def __init__(self):
        self._display = None
        self._X = None
        self._XK = None
        self._xtest = None
        self._event = None
        try:
            from Xlib import X, XK, display
            from Xlib.ext import xtest
            from Xlib.protocol import event
            self._display = display.Display()
            self._X = X
            self._XK = XK
            self._xtest = xtest
            self._event = event
        except Exception as exc:
            logger.debug(
                "Linux X11 input backend unavailable (exception_type=%s)",
                type(exc).__name__,
            )

    def is_available(self) -> bool:
        return self._display is not None

    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        try:
            keycodes: list[int] = []
            for char in text:
                keysym = self._XK.string_to_keysym(char)
                if not keysym:
                    # Use Unicode keysym for chars not in standard X keysym table.
                    keysym = ord(char) | 0x01000000
                keycode = self._display.keysym_to_keycode(keysym)
                if not keycode:
                    # Preflight the complete value so an unsupported character
                    # cannot leave a partially typed prefix behind.
                    return False
                keycodes.append(keycode)

            dispatched = False
            for char, keycode in zip(text, keycodes):
                self._xtest.fake_input(self._display, self._X.KeyPress, keycode)
                self._xtest.fake_input(self._display, self._X.KeyRelease, keycode)
                dispatched = True
                if human_like:
                    if dispatched:
                        self._display.sync()
                        dispatched = False
                    d = delay + random.uniform(-0.005, 0.015)
                    if char in " .,;:!?\n":
                        d *= random.uniform(1.3, 2.0)
                    time.sleep(max(0, d))
            if dispatched:
                self._display.sync()
            return True
        except Exception:
            logger.error("Linux type_text failed")
            return False

    # XK name mapping for special keys
    _KEY_MAP = {
        "return": "Return", "enter": "Return", "tab": "Tab",
        "escape": "Escape", "esc": "Escape",
        "backspace": "BackSpace", "delete": "BackSpace",
        "forward_delete": "Delete",
        "space": "space",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End",
        "pageup": "Prior", "page_up": "Prior",
        "pagedown": "Next", "page_down": "Next",
        "control": "Control_L", "ctrl": "Control_L",
        "shift": "Shift_L",
        "alt": "Alt_L", "option": "Alt_L",
        "command": "Super_L",
        "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
        "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
        "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    }

    def press_key(self, key: str) -> bool:
        try:
            xk_name = self._KEY_MAP.get(key.lower(), key)
            keysym = self._XK.string_to_keysym(xk_name)
            if not keysym:
                logger.error(
                    "Linux press_key: unknown key (key_length=%d)",
                    len(key),
                )
                return False
            keycode = self._display.keysym_to_keycode(keysym)
            if not keycode:
                logger.error(
                    "Linux press_key: no keycode (key_length=%d)",
                    len(key),
                )
                return False
            self._xtest.fake_input(self._display, self._X.KeyPress, keycode)
            self._xtest.fake_input(self._display, self._X.KeyRelease, keycode)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux press_key failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def hotkey(self, *keys: str) -> bool:
        try:
            keycodes = []
            for key in keys:
                xk_name = self._KEY_MAP.get(key.lower(), key)
                keysym = self._XK.string_to_keysym(xk_name)
                if not keysym:
                    logger.error(
                        "Linux hotkey: unknown key (key_length=%d)",
                        len(key),
                    )
                    return False
                kc = self._display.keysym_to_keycode(keysym)
                if not kc:
                    logger.error(
                        "Linux hotkey: no keycode (key_length=%d)",
                        len(key),
                    )
                    return False
                keycodes.append(kc)
            for kc in keycodes:
                self._xtest.fake_input(self._display, self._X.KeyPress, kc)
            for kc in reversed(keycodes):
                self._xtest.fake_input(self._display, self._X.KeyRelease, kc)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux hotkey failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        btn_map = {"left": 1, "middle": 2, "right": 3}
        btn = btn_map.get(button, 1)
        try:
            root = self._display.screen().root
            root.warp_pointer(x, y)
            self._display.sync()
            self._xtest.fake_input(self._display, self._X.ButtonPress, btn)
            self._xtest.fake_input(self._display, self._X.ButtonRelease, btn)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux click failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def double_click(self, x: int, y: int) -> bool:
        try:
            root = self._display.screen().root
            root.warp_pointer(x, y)
            self._display.sync()
            for _ in range(2):
                self._xtest.fake_input(self._display, self._X.ButtonPress, 1)
                self._xtest.fake_input(self._display, self._X.ButtonRelease, 1)
                self._display.sync()
                time.sleep(0.05)
            return True
        except Exception as e:
            logger.error(
                "Linux double_click failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def move_mouse(self, x: int, y: int) -> bool:
        try:
            root = self._display.screen().root
            root.warp_pointer(x, y)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux move_mouse failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def scroll(self, x: int, y: int, delta_x: int = 0, delta_y: int = -3) -> bool:
        # X11 button 4 = scroll up, button 5 = scroll down
        # delta_y negative = down (button 5), positive = up (button 4)
        try:
            root = self._display.screen().root
            root.warp_pointer(x, y)
            self._display.sync()
            if delta_y != 0:
                btn = 5 if delta_y < 0 else 4
                amount = abs(delta_y)
                for _ in range(amount):
                    self._xtest.fake_input(self._display, self._X.ButtonPress, btn)
                    self._xtest.fake_input(self._display, self._X.ButtonRelease, btn)
            if delta_x != 0:
                # X11 button 6 = scroll left, button 7 = scroll right
                btn = 7 if delta_x > 0 else 6
                amount = abs(delta_x)
                for _ in range(amount):
                    self._xtest.fake_input(self._display, self._X.ButtonPress, btn)
                    self._xtest.fake_input(self._display, self._X.ButtonRelease, btn)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux scroll failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> bool:
        try:
            root = self._display.screen().root
            root.warp_pointer(from_x, from_y)
            self._display.sync()
            self._xtest.fake_input(self._display, self._X.ButtonPress, 1)
            self._display.sync()
            steps = max(abs(to_x - from_x), abs(to_y - from_y), 1)
            steps = min(steps, 20)
            for i in range(1, steps + 1):
                ix = from_x + (to_x - from_x) * i // steps
                iy = from_y + (to_y - from_y) * i // steps
                root.warp_pointer(ix, iy)
                self._display.sync()
            self._xtest.fake_input(self._display, self._X.ButtonRelease, 1)
            self._display.sync()
            return True
        except Exception as e:
            logger.error(
                "Linux drag failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def paste_text(self, text: str) -> bool:
        """Paste text via Ctrl+V after setting clipboard via xclip/xsel if available."""
        return self.hotkey("ctrl", "v")

    def activate_window(self, pid: int) -> bool:
        """Request foreground activation through the EWMH window-manager API."""
        try:
            if self.is_frontmost(pid):
                return True
            target = self._window_for_pid(pid)
            if target is None or self._event is None:
                return False

            root = self._display.screen().root
            active_atom = self._display.intern_atom("_NET_ACTIVE_WINDOW")
            current = self._property_values(root, "_NET_ACTIVE_WINDOW")
            current_window = int(current[0]) if current else 0
            message = self._event.ClientMessage(
                window=target,
                client_type=active_atom,
                data=(32, [1, self._X.CurrentTime, current_window, 0, 0]),
            )
            root.send_event(
                message,
                event_mask=(
                    self._X.SubstructureRedirectMask
                    | self._X.SubstructureNotifyMask
                ),
                propagate=False,
            )
            self._display.flush()
            return True
        except Exception as e:
            logger.error(
                "Linux activate_window failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def _property_values(self, window, property_name: str) -> list[int]:
        """Read one EWMH property as integer values."""
        atom = self._display.intern_atom(property_name, only_if_exists=True)
        if not atom:
            return []
        value = window.get_full_property(atom, self._X.AnyPropertyType)
        if value is None:
            return []
        return [int(item) for item in value.value]

    def _window_pid(self, window) -> int | None:
        values = self._property_values(window, "_NET_WM_PID")
        return values[0] if values else None

    def _window_for_pid(self, pid: int):
        root = self._display.screen().root
        for property_name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            for window_id in reversed(self._property_values(root, property_name)):
                try:
                    window = self._display.create_resource_object("window", window_id)
                    if self._window_pid(window) == pid:
                        return window
                except Exception:
                    continue
        return None

    def is_frontmost(self, pid: int) -> bool:
        """Return whether the EWMH active window belongs to ``pid``."""
        try:
            root = self._display.screen().root
            active = self._property_values(root, "_NET_ACTIVE_WINDOW")
            if not active:
                return False
            window = self._display.create_resource_object("window", active[0])
            return self._window_pid(window) == pid
        except Exception:
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
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes  # noqa: F401
            return True
        except ImportError:
            return False

    def _send_input(self, inp) -> bool:
        import ctypes
        sent = self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        return int(sent) == 1

    def _send_inputs(self, inputs: list[object]) -> bool:
        """Send one uninterrupted native input array and verify full dispatch."""
        import ctypes

        if not inputs:
            return True
        array = (self._INPUT * len(inputs))(*inputs)
        sent = self._user32.SendInput(
            len(inputs),
            array,
            ctypes.sizeof(self._INPUT),
        )
        return int(sent) == len(inputs)

    def _unicode_input_events(self, text: str) -> list[object]:
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        events: list[object] = []
        for scan in _utf16_code_units(text):
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                events.append(
                    self._INPUT(
                        type=1,
                        _input=self._INPUT._INPUT(
                            ki=self._KEYBDINPUT(
                                wVk=0,
                                wScan=scan,
                                dwFlags=flags,
                            )
                        ),
                    )
                )
        return events

    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        self._load()
        try:
            if not human_like:
                return self._send_inputs(self._unicode_input_events(text))

            for character in text:
                if not self._send_inputs(self._unicode_input_events(character)):
                    return False
                d = delay * random.uniform(0.7, 1.3)
                if character in " .,;:!?\n":
                    d *= random.uniform(1.3, 2.0)
                time.sleep(max(0, d))
            return True
        except Exception:
            logger.error("Windows type_text failed")
            return False

    def _key_input(self, vk: int, *, key_up: bool = False):
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_EXTENDEDKEY = 0x0001
        flags = KEYEVENTF_EXTENDEDKEY if vk in self._EXTENDED else 0
        if key_up:
            flags |= KEYEVENTF_KEYUP
        return self._INPUT(
            type=1,
            _input=self._INPUT._INPUT(ki=self._KEYBDINPUT(
                wVk=vk, dwFlags=flags,
            )),
        )

    def _tap_vk(self, vk: int) -> bool:
        return self._send_inputs(
            [self._key_input(vk), self._key_input(vk, key_up=True)]
        )

    def _press_vk(self, vk: int) -> bool:
        return self._send_input(self._key_input(vk))

    def _release_vk(self, vk: int) -> bool:
        return self._send_input(self._key_input(vk, key_up=True))

    def press_key(self, key: str) -> bool:
        self._load()
        vk = self._VK.get(key.lower())
        if vk is None:
            return False
        try:
            return self._tap_vk(vk)
        except Exception as e:
            logger.error(
                "Windows press_key failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def hotkey(self, *keys: str) -> bool:
        self._load()
        vks = [self._VK.get(k.lower()) for k in keys]
        if any(v is None for v in vks):
            return False
        try:
            events = [self._key_input(vk) for vk in vks[:-1]]
            events.extend(
                [
                    self._key_input(vks[-1]),
                    self._key_input(vks[-1], key_up=True),
                ]
            )
            events.extend(
                self._key_input(vk, key_up=True) for vk in reversed(vks[:-1])
            )
            return self._send_inputs(events)
        except Exception as e:
            logger.error(
                "Windows hotkey failed (exception_type=%s)",
                type(e).__name__,
            )
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

            return self._send_inputs(
                [
                    self._mouse_input(
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                        dx=nx,
                        dy=ny,
                    ),
                    self._mouse_input(down_flag),
                    self._mouse_input(up_flag),
                ]
            )
        except Exception as e:
            logger.error(
                "Windows click failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def _mouse_input(
        self,
        flags: int,
        *,
        data: int = 0,
        dx: int = 0,
        dy: int = 0,
    ):
        return self._INPUT(
            type=0,
            _input=self._INPUT._INPUT(
                mi=self._MOUSEINPUT(
                    dx=dx,
                    dy=dy,
                    dwFlags=flags,
                    mouseData=data,
                )
            ),
        )

    def _move_input(self, x: int, y: int):
        """Move mouse to absolute position using normalized coords."""
        screen_w = self._user32.GetSystemMetrics(0)
        screen_h = self._user32.GetSystemMetrics(1)
        if screen_w <= 0 or screen_h <= 0:
            raise RuntimeError("invalid Windows screen metrics")
        nx = int(x * 65535 / screen_w)
        ny = int(y * 65535 / screen_h)
        return self._mouse_input(0x0001 | 0x8000, dx=nx, dy=ny)

    def _move_to(self, x: int, y: int) -> bool:
        return self._send_input(self._move_input(x, y))

    def _mouse_event(self, flags: int, data: int = 0) -> bool:
        """Send a mouse event with given flags."""
        return self._send_input(self._mouse_input(flags, data=data))

    def scroll(self, x: int, y: int, delta_x: int = 0, delta_y: int = -3) -> bool:
        """Scroll using SendInput mouse wheel events."""
        self._load()
        try:
            MOUSEEVENTF_WHEEL = 0x0800
            WHEEL_DELTA = 120
            # delta_y negative = scroll down, positive = scroll up
            amount = min(abs(delta_y) if delta_y != 0 else 3, 20)
            wheel_delta = -WHEEL_DELTA if delta_y <= 0 else WHEEL_DELTA
            events = [self._move_input(x, y)]
            events.extend(
                self._mouse_input(MOUSEEVENTF_WHEEL, data=wheel_delta)
                for _ in range(amount)
            )
            return self._send_inputs(events)
        except Exception as e:
            logger.error(
                "Windows scroll failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> bool:
        """Drag using SendInput mouse events."""
        self._load()
        try:
            steps = min(max(abs(to_x - from_x), abs(to_y - from_y), 1), 20)
            events = [
                self._move_input(from_x, from_y),
                self._mouse_input(0x0002),
            ]
            for i in range(1, steps + 1):
                ix = from_x + (to_x - from_x) * i // steps
                iy = from_y + (to_y - from_y) * i // steps
                events.append(self._move_input(ix, iy))
            events.append(self._mouse_input(0x0004))
            return self._send_inputs(events)
        except Exception as e:
            logger.error(
                "Windows drag failed (exception_type=%s)",
                type(e).__name__,
            )
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
                return bool(SetForegroundWindow(target_hwnd))
            return False
        except Exception as e:
            logger.error(
                "Windows activate_window failed (exception_type=%s)",
                type(e).__name__,
            )
            return False

    def is_frontmost(self, pid: int) -> bool:
        """Return whether the Win32 foreground window belongs to ``pid``."""
        self._load()
        try:
            import ctypes
            from ctypes import wintypes

            get_foreground_window = self._user32.GetForegroundWindow
            get_foreground_window.restype = wintypes.HWND
            hwnd = get_foreground_window()
            if not hwnd:
                return False
            process_id = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            return process_id.value == pid
        except Exception as e:
            logger.error(
                "Windows foreground check failed (exception_type=%s)",
                type(e).__name__,
            )
            return False


# ── Fallback: AppleScript (macOS only, simpler) ───────────────────

class AppleScriptInputBackend(InputBackend):
    """Fallback macOS input using osascript System Events (slower but no PyObjC)."""

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            completed = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get UI elements enabled',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return (
                completed.returncode == 0
                and completed.stdout.strip().casefold() == "true"
            )
        except Exception:
            return False

    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        try:
            if not text:
                return True
            import json

            if human_like:
                script = f'''
                var se = Application("System Events");
                var text = {json.dumps(text)};
                var chars = Array.from(text);
                for (var i = 0; i < chars.length; i++) {{
                    se.keystroke(chars[i]);
                    delay({max(0, delay)});
                }}
                '''
                timeout = max(30, len(text) * max(0, delay) * 2)
            else:
                script = f'''
                var se = Application("System Events");
                var text = {json.dumps(text)};
                se.keystroke(text);
                '''
                timeout = 30

            completed = subprocess.run(
                ["osascript", "-l", "JavaScript"],
                input=script,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return completed.returncode == 0
        except Exception:
            logger.error("AppleScript type_text failed")
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
            completed = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to key code {code}'],
                capture_output=True, timeout=5,
            )
            return completed.returncode == 0
        except Exception:
            return False

    def hotkey(self, *keys: str) -> bool:
        if len(keys) < 2:
            return False
        modifiers = {
            "command": "command down", "shift": "shift down",
            "option": "option down", "alt": "option down",
            "control": "control down", "ctrl": "control down",
        }
        mods = [modifiers.get(k.lower()) for k in keys[:-1]]
        if any(mod is None for mod in mods):
            return False
        final_key = keys[-1]
        if len(final_key) != 1 or not mods:
            return False
        mod_str = ", ".join(mods)
        try:
            import json

            completed = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to keystroke {json.dumps(final_key)} '
                 f'using {{{mod_str}}}'],
                capture_output=True, timeout=5,
            )
            return completed.returncode == 0
        except Exception:
            return False

    def click(self, x: int, y: int, button: str = "left") -> bool:
        # AppleScript can't easily do coordinate clicks; not supported
        return False

    def scroll(self, x: int, y: int, delta_x: int = 0, delta_y: int = -3) -> bool:
        return False

    def activate_window(self, pid: int) -> bool:
        try:
            completed = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {pid}) to true'],
                capture_output=True, timeout=5,
            )
            return completed.returncode == 0
        except Exception:
            return False

    def is_frontmost(self, pid: int) -> bool:
        try:
            completed = subprocess.run(
                [
                    "osascript",
                    "-e",
                    "tell application \"System Events\" to get unix id of first process whose frontmost is true",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return completed.returncode == 0 and completed.stdout.strip() == str(pid)
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
        if backend.is_available():
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
            logger.info("Input backend: Linux X11/XTest")
            return backend

    # Ultimate fallback — return a no-op that always fails
    logger.warning("No input backend available")
    return _NoOpBackend()


class _NoOpBackend(InputBackend):
    def is_available(self) -> bool:
        return False
    def type_text(self, text, delay=0.02, human_like=False) -> bool:
        return False
    def press_key(self, key) -> bool:
        return False
    def hotkey(self, *keys) -> bool:
        return False
    def click(self, x, y, button="left") -> bool:
        return False
    def scroll(self, x, y, delta_x=0, delta_y=-3) -> bool:
        return False
    def activate_window(self, pid) -> bool:
        return False
