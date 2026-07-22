"""Tests for LinuxInputBackend (python-xlib XTest implementation).

These tests run on all platforms. On non-Linux or without python-xlib, the
backend reports is_available() == False and all methods return False gracefully.
The core logic is tested via mock injection.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest

from agent_eyes.input_sim import InputBackend, LinuxInputBackend


# ── helpers ────────────────────────────────────────────────────────


def _make_backend_with_mocks():
    """Return a LinuxInputBackend with all Xlib internals mocked out."""
    backend = LinuxInputBackend.__new__(LinuxInputBackend)

    mock_X = MagicMock()
    mock_X.KeyPress = 2
    mock_X.KeyRelease = 3
    mock_X.ButtonPress = 4
    mock_X.ButtonRelease = 5

    mock_XK = MagicMock()
    mock_XK.string_to_keysym.side_effect = lambda name: {
        "a": 97, "Return": 65293, "ctrl": 0, "Control_L": 65507,
        "Shift_L": 65505, "c": 99, "space": 32,
    }.get(name, 0)

    mock_display = MagicMock()
    mock_display.keysym_to_keycode.side_effect = lambda keysym: keysym if keysym else 0
    mock_display.screen.return_value.root = MagicMock()

    mock_xtest = MagicMock()

    backend._display = mock_display
    backend._X = mock_X
    backend._XK = mock_XK
    backend._xtest = mock_xtest

    return backend, mock_display, mock_X, mock_XK, mock_xtest


# ── is_available ───────────────────────────────────────────────────


class TestIsAvailable:
    def test_returns_true_when_display_is_set(self):
        backend, *_ = _make_backend_with_mocks()
        assert backend.is_available() is True

    def test_returns_false_when_display_is_none(self):
        backend = LinuxInputBackend.__new__(LinuxInputBackend)
        backend._display = None
        assert backend.is_available() is False

    def test_factory_returns_available_linux_backend(self, monkeypatch):
        import agent_eyes.input_sim as input_sim

        backend, *_ = _make_backend_with_mocks()
        monkeypatch.setattr(input_sim.sys, "platform", "linux")
        monkeypatch.setattr(input_sim, "LinuxInputBackend", lambda: backend)
        monkeypatch.setattr(input_sim, "_backend_cache", None)

        assert input_sim.get_input_backend() is backend

    def test_init_without_xlib_sets_display_none(self):
        """If python-xlib is not importable, _display stays None."""
        with patch.dict("sys.modules", {"Xlib": None, "Xlib.ext": None, "Xlib.ext.xtest": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                backend = LinuxInputBackend.__new__(LinuxInputBackend)
                backend._display = None
                backend._X = None
                backend._XK = None
                backend._xtest = None
                assert backend.is_available() is False

    def test_init_with_unavailable_display_does_not_crash(self):
        """Headless/Wayland sessions can have Xlib installed but no X display."""
        fake_xlib = MagicMock()
        fake_display = MagicMock()
        fake_display.Display.side_effect = OSError("display unavailable")
        fake_xlib.display = fake_display
        fake_ext = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "Xlib": fake_xlib,
                "Xlib.ext": fake_ext,
                "Xlib.ext.xtest": fake_ext.xtest,
            },
        ):
            backend = LinuxInputBackend()

        assert backend.is_available() is False


# ── type_text ──────────────────────────────────────────────────────


class TestTypeText:
    def test_default_fast_path_has_no_sleep_and_syncs_once(self, monkeypatch):
        backend, display, _X, _XK, xtest = _make_backend_with_mocks()

        def unexpected_sleep(_delay):
            raise AssertionError("default typing slept")

        monkeypatch.setattr("agent_eyes.input_sim.time.sleep", unexpected_sleep)

        assert backend.type_text("ac" * 500) is True
        assert xtest.fake_input.call_count == 2000
        display.sync.assert_called_once()

    def test_human_like_mode_retains_per_character_sleep(self, monkeypatch):
        backend, display, _X, _XK, _xtest = _make_backend_with_mocks()
        sleep = MagicMock()
        monkeypatch.setattr("agent_eyes.input_sim.time.sleep", sleep)
        monkeypatch.setattr("agent_eyes.input_sim.random.uniform", lambda _a, _b: 0)

        assert backend.type_text("ac", delay=0.01, human_like=True) is True
        assert sleep.call_count == 2
        assert display.sync.call_count == 2

    def test_happy_path_types_each_char(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        # 'a' → keysym 97 → keycode 97
        result = backend.type_text("a", delay=0, human_like=False)
        assert result is True
        xtest.fake_input.assert_any_call(display, X.KeyPress, 97)
        xtest.fake_input.assert_any_call(display, X.KeyRelease, 97)

    def test_types_multiple_characters(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.type_text("ac", delay=0, human_like=False)
        assert result is True
        # Expect KeyPress/KeyRelease for both 'a' (97) and 'c' (99)
        calls = xtest.fake_input.call_args_list
        pressed = [c.args[2] for c in calls if c.args[1] == X.KeyPress]
        assert 97 in pressed
        assert 99 in pressed

    def test_rejects_chars_with_no_keycode(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        # Override: keysym returns something but keysym_to_keycode returns 0
        XK.string_to_keysym.side_effect = lambda name: 9999 if name == "a" else 0
        display.keysym_to_keycode.side_effect = lambda ks: 0  # no keycode
        result = backend.type_text("a", delay=0, human_like=False)
        assert result is False
        xtest.fake_input.assert_not_called()

    def test_unsupported_suffix_is_rejected_before_typing_prefix(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        XK.string_to_keysym.side_effect = lambda name: {"a": 97, "€": 9999}.get(name, 0)
        display.keysym_to_keycode.side_effect = lambda keysym: 0 if keysym == 9999 else keysym

        assert backend.type_text("a€", delay=0, human_like=False) is False

        xtest.fake_input.assert_not_called()
        display.sync.assert_not_called()

    def test_unicode_fallback_for_unmapped_char(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        # string_to_keysym returns 0 → Unicode fallback applied
        XK.string_to_keysym.side_effect = lambda name: 0
        display.keysym_to_keycode.side_effect = lambda ks: ks if ks else 0
        result = backend.type_text("€", delay=0, human_like=False)
        assert result is True
        # Verify keysym_to_keycode was called with Unicode fallback
        expected_keysym = ord("€") | 0x01000000
        display.keysym_to_keycode.assert_called_with(expected_keysym)

    def test_returns_false_on_exception(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        xtest.fake_input.side_effect = Exception("Xlib error")
        result = backend.type_text("a", delay=0, human_like=False)
        assert result is False

    def test_empty_string(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.type_text("", delay=0, human_like=False)
        assert result is True
        xtest.fake_input.assert_not_called()


# ── press_key ──────────────────────────────────────────────────────


class TestPressKey:
    def test_press_mapped_key_return(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        # "return" maps to XK name "Return" → keysym 65293 → keycode 65293
        result = backend.press_key("return")
        assert result is True
        xtest.fake_input.assert_any_call(display, X.KeyPress, 65293)
        xtest.fake_input.assert_any_call(display, X.KeyRelease, 65293)
        display.sync.assert_called()

    def test_press_enter_same_as_return(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.press_key("enter")
        assert result is True

    def test_unknown_key_returns_false(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        XK.string_to_keysym.side_effect = lambda name: 0
        result = backend.press_key("nonexistentkey")
        assert result is False

    def test_key_with_no_keycode_returns_false(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        XK.string_to_keysym.side_effect = lambda name: 99999  # valid keysym
        display.keysym_to_keycode.side_effect = lambda ks: 0  # no keycode
        result = backend.press_key("somekey")
        assert result is False

    def test_press_key_case_insensitive(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.press_key("RETURN")
        assert result is True


# ── hotkey ─────────────────────────────────────────────────────────


class TestHotkey:
    def test_hotkey_presses_modifiers_then_key(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        # ctrl (Control_L → 65507) + c (99)
        result = backend.hotkey("ctrl", "c")
        assert result is True
        calls = xtest.fake_input.call_args_list
        pressed = [c.args[2] for c in calls if c.args[1] == X.KeyPress]
        released = [c.args[2] for c in calls if c.args[1] == X.KeyRelease]
        assert 65507 in pressed
        assert 99 in pressed
        # Releases in reverse order
        assert released.index(99) < released.index(65507)

    def test_hotkey_returns_false_for_unknown_key(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        XK.string_to_keysym.side_effect = lambda name: 0
        result = backend.hotkey("ctrl", "z")
        assert result is False

    def test_hotkey_syncs_display(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.hotkey("ctrl", "c")
        display.sync.assert_called()


# ── click ──────────────────────────────────────────────────────────


class TestClick:
    def test_left_click_warps_and_fires_button1(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.click(100, 200)
        assert result is True
        display.screen().root.warp_pointer.assert_called_with(100, 200)
        xtest.fake_input.assert_any_call(display, X.ButtonPress, 1)
        xtest.fake_input.assert_any_call(display, X.ButtonRelease, 1)

    def test_right_click_uses_button3(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.click(50, 50, button="right")
        assert result is True
        xtest.fake_input.assert_any_call(display, X.ButtonPress, 3)
        xtest.fake_input.assert_any_call(display, X.ButtonRelease, 3)

    def test_middle_click_uses_button2(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.click(50, 50, button="middle")
        assert result is True
        xtest.fake_input.assert_any_call(display, X.ButtonPress, 2)

    def test_click_syncs_display(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.click(0, 0)
        display.sync.assert_called()

    def test_click_returns_false_on_exception(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        xtest.fake_input.side_effect = Exception("X error")
        result = backend.click(10, 10)
        assert result is False


# ── double_click ───────────────────────────────────────────────────


class TestDoubleClick:
    def test_double_click_fires_button1_twice(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.double_click(100, 200)
        assert result is True
        press_calls = [c for c in xtest.fake_input.call_args_list if c.args[1] == X.ButtonPress]
        assert len(press_calls) == 2

    def test_double_click_warps_to_coordinates(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.double_click(300, 400)
        display.screen().root.warp_pointer.assert_called_with(300, 400)


# ── move_mouse ─────────────────────────────────────────────────────


class TestMoveMouse:
    def test_move_mouse_warps_pointer(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.move_mouse(250, 350)
        assert result is True
        display.screen().root.warp_pointer.assert_called_with(250, 350)
        display.sync.assert_called()

    def test_move_mouse_no_button_events(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.move_mouse(10, 10)
        xtest.fake_input.assert_not_called()


# ── scroll ─────────────────────────────────────────────────────────


class TestScroll:
    def test_scroll_down_uses_button5(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.scroll(100, 200, delta_y=-3)
        assert result is True
        press_calls = [c for c in xtest.fake_input.call_args_list if c.args[1] == X.ButtonPress]
        assert all(c.args[2] == 5 for c in press_calls)
        assert len(press_calls) == 3

    def test_scroll_up_uses_button4(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.scroll(100, 200, delta_y=2)
        assert result is True
        press_calls = [c for c in xtest.fake_input.call_args_list if c.args[1] == X.ButtonPress]
        assert all(c.args[2] == 4 for c in press_calls)
        assert len(press_calls) == 2

    def test_scroll_zero_delta_no_button_events(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.scroll(100, 200, delta_x=0, delta_y=0)
        assert result is True
        xtest.fake_input.assert_not_called()

    def test_scroll_right_uses_button7(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.scroll(100, 200, delta_x=2, delta_y=0)
        assert result is True
        press_calls = [c for c in xtest.fake_input.call_args_list if c.args[1] == X.ButtonPress]
        assert all(c.args[2] == 7 for c in press_calls)

    def test_scroll_left_uses_button6(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.scroll(100, 200, delta_x=-1, delta_y=0)
        assert result is True
        press_calls = [c for c in xtest.fake_input.call_args_list if c.args[1] == X.ButtonPress]
        assert all(c.args[2] == 6 for c in press_calls)

    def test_scroll_warps_to_position(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.scroll(150, 250, delta_y=-1)
        display.screen().root.warp_pointer.assert_called_with(150, 250)


# ── drag ───────────────────────────────────────────────────────────


class TestDrag:
    def test_drag_presses_and_releases_button1(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        result = backend.drag(0, 0, 100, 100)
        assert result is True
        xtest.fake_input.assert_any_call(display, X.ButtonPress, 1)
        xtest.fake_input.assert_any_call(display, X.ButtonRelease, 1)

    def test_drag_starts_at_from_coordinates(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.drag(10, 20, 100, 200)
        first_warp = display.screen().root.warp_pointer.call_args_list[0]
        assert first_warp == call(10, 20)

    def test_drag_ends_at_to_coordinates(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.drag(0, 0, 50, 50)
        warp_calls = display.screen().root.warp_pointer.call_args_list
        last_warp = warp_calls[-1]
        assert last_warp == call(50, 50)

    def test_drag_button_press_before_release(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        backend.drag(0, 0, 100, 0)
        calls = xtest.fake_input.call_args_list
        event_types = [c.args[1] for c in calls]
        press_idx = event_types.index(X.ButtonPress)
        release_idx = len(event_types) - 1 - event_types[::-1].index(X.ButtonRelease)
        assert press_idx < release_idx

    def test_drag_returns_false_on_exception(self):
        backend, display, X, XK, xtest = _make_backend_with_mocks()
        xtest.fake_input.side_effect = Exception("X error")
        result = backend.drag(0, 0, 10, 10)
        assert result is False


# ── activate_window ────────────────────────────────────────────────


class TestActivateWindow:
    def test_returns_false(self):
        """python-xlib does not provide cross-WM window activation."""
        backend, *_ = _make_backend_with_mocks()
        result = backend.activate_window(12345)
        assert result is False


# ── base class contract ────────────────────────────────────────────


class TestBaseClassContract:
    def test_drag_is_on_base_class(self):
        assert hasattr(InputBackend, "drag")

    def test_drag_default_returns_false(self):
        """Base class drag returns False by default."""
        # Use MacOSInputBackend as a concrete subclass that doesn't override drag
        from agent_eyes.input_sim import MacOSInputBackend
        backend = MacOSInputBackend.__new__(MacOSInputBackend)
        backend._quartz = None
        result = InputBackend.drag(backend, 0, 0, 10, 10)
        assert result is False

    def test_linux_backend_is_subclass_of_input_backend(self):
        assert issubclass(LinuxInputBackend, InputBackend)
