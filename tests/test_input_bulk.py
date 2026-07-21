from __future__ import annotations

import ctypes
import inspect
import json
import logging
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent_eyes.input_sim as input_sim


class _FakeQuartz:
    kCGEventSourceStateHIDSystemState = 1
    kCGSessionEventTap = 2

    def __init__(self) -> None:
        self.payloads: list[tuple[int, str]] = []
        self.posts: list[dict[str, object]] = []

    def CGEventSourceCreate(self, state: int) -> object:
        assert state == self.kCGEventSourceStateHIDSystemState
        return object()

    def CGEventCreateKeyboardEvent(
        self,
        source: object,
        key_code: int,
        key_down: bool,
    ) -> dict[str, object]:
        assert source is not None
        assert key_code == 0
        return {"key_down": key_down}

    def CGEventKeyboardSetUnicodeString(
        self,
        event: dict[str, object],
        length: int,
        text: str,
    ) -> None:
        event["length"] = length
        event["text"] = text
        self.payloads.append((length, text))

    def CGEventPost(self, tap: int, event: dict[str, object]) -> None:
        assert tap == self.kCGSessionEventTap
        self.posts.append(event)
class _FakeKeyboardInput:
    def __init__(self, *, wVk: int = 0, wScan: int = 0, dwFlags: int = 0) -> None:
        self.wVk = wVk
        self.wScan = wScan
        self.dwFlags = dwFlags


class _FakeInputUnion:
    def __init__(self, *, ki: _FakeKeyboardInput) -> None:
        self.ki = ki


class _FakeInput:
    _INPUT = _FakeInputUnion

    def __init__(self, *, type: int, _input: _FakeInputUnion) -> None:
        self.type = type
        self._input = _input


def _make_windows_backend() -> input_sim.WindowsInputBackend:
    backend = input_sim.WindowsInputBackend.__new__(input_sim.WindowsInputBackend)
    backend._load = MagicMock()
    backend._INPUT = _FakeInput
    backend._KEYBDINPUT = _FakeKeyboardInput
    backend._send_inputs = MagicMock(return_value=True)
    backend._send_input = MagicMock(return_value=True)
    return backend


@pytest.mark.parametrize(
    "backend_type",
    [
        input_sim.InputBackend,
        input_sim.MacOSInputBackend,
        input_sim.LinuxInputBackend,
        input_sim.WindowsInputBackend,
        input_sim.AppleScriptInputBackend,
        input_sim._NoOpBackend,
    ],
)
def test_fast_mode_is_the_default_type_contract(backend_type):
    signature = inspect.signature(backend_type.type_text)

    assert signature.parameters["human_like"].default is False


def test_composite_clear_and_click_typing_have_no_unconditional_sleep(monkeypatch):
    calls: list[object] = []

    class Probe:
        clear_field = input_sim.InputBackend.clear_field
        clear_and_type = input_sim.InputBackend.clear_and_type
        click_and_type = input_sim.InputBackend.click_and_type

        def select_all(self):
            calls.append("select_all")
            return True

        def press_key(self, key):
            calls.append(("press_key", key))
            return True

        def type_text(self, text, delay=0.02):
            calls.append(("type_text", text, delay))
            return True

        def click(self, x, y):
            calls.append(("click", x, y))
            return True

    monkeypatch.setattr(
        input_sim.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(AssertionError("composite input slept")),
    )
    probe = Probe()

    assert probe.clear_and_type("hello") is True
    assert probe.click_and_type(10, 20, "world") is True
    assert calls == [
        "select_all",
        ("press_key", "delete"),
        ("type_text", "hello", 0.02),
        ("click", 10, 20),
        "select_all",
        ("press_key", "delete"),
        ("type_text", "world", 0.02),
    ]


def test_utf16_code_units_preserve_supplementary_characters():
    assert input_sim._utf16_code_units("A😀€") == (0x41, 0xD83D, 0xDE00, 0x20AC)


def test_macos_default_uses_bounded_bulk_events_without_sleep(monkeypatch):
    quartz = _FakeQuartz()
    backend = input_sim.MacOSInputBackend()
    backend._quartz = quartz
    text = "a" * 19 + "😀" + "z" * 21

    def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("default typing slept")

    monkeypatch.setattr(input_sim.time, "sleep", unexpected_sleep)

    assert backend.type_text(text) is True

    down_payloads = quartz.payloads[::2]
    up_payloads = quartz.payloads[1::2]
    assert down_payloads == up_payloads
    assert "".join(payload for _, payload in down_payloads) == text
    assert all(length <= 20 for length, _ in down_payloads)
    assert all(
        length == len(payload.encode("utf-16-le")) // 2
        for length, payload in down_payloads
    )
    assert len(quartz.posts) == len(down_payloads) * 2


def test_macos_installed_quartz_roundtrips_bulk_unicode_without_posting():
    quartz = pytest.importorskip("Quartz")
    text = "Upper ! AltGr € dead é supplementary 😀" * 20
    chunks = input_sim._chunks_by_utf16_units(text, 20)
    actual: list[str] = []

    for chunk in chunks:
        unit_count = len(input_sim._utf16_code_units(chunk))
        event = quartz.CGEventCreateKeyboardEvent(None, 0, True)
        quartz.CGEventKeyboardSetUnicodeString(event, unit_count, chunk)
        actual_count, value = quartz.CGEventKeyboardGetUnicodeString(
            event,
            unit_count,
            None,
            None,
        )
        assert actual_count == unit_count
        actual.append(value)

    assert "".join(actual) == text


def test_macos_human_like_mode_retains_per_character_delay(monkeypatch):
    quartz = _FakeQuartz()
    backend = input_sim.MacOSInputBackend()
    backend._quartz = quartz
    sleeps: list[float] = []
    monkeypatch.setattr(input_sim.time, "sleep", sleeps.append)
    monkeypatch.setattr(input_sim.random, "uniform", lambda _low, _high: 1.0)

    assert backend.type_text("a😀", delay=0.01, human_like=True) is True

    assert [payload for _, payload in quartz.payloads[::2]] == ["a", "😀"]
    assert [length for length, _ in quartz.payloads[::2]] == [1, 2]
    assert sleeps == [0.01, 0.01]


@pytest.mark.parametrize("permitted", [True, False])
def test_macos_availability_preflights_post_event_permission(monkeypatch, permitted):
    preflight = MagicMock(return_value=permitted)
    monkeypatch.setattr(input_sim.sys, "platform", "darwin")
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(CGPreflightPostEventAccess=preflight),
    )

    assert input_sim.MacOSInputBackend().is_available() is permitted

    preflight.assert_called_once_with()


def test_macos_activation_fallback_propagates_osascript_failure(monkeypatch):
    running_application = MagicMock()
    running_application.runningApplicationWithProcessIdentifier_.return_value = None
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSRunningApplication=running_application,
            NSApplicationActivateIgnoringOtherApps=2,
        ),
    )
    runner = MagicMock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr(input_sim.subprocess, "run", runner)

    assert input_sim.MacOSInputBackend().activate_window(42) is False

    assert runner.call_args.kwargs["timeout"] == 3


def test_macos_type_failure_does_not_log_typed_content(caplog):
    secret = "sentinel-secret-macos"
    quartz = _FakeQuartz()
    quartz.CGEventKeyboardSetUnicodeString = MagicMock(
        side_effect=RuntimeError(secret)
    )
    backend = input_sim.MacOSInputBackend()
    backend._quartz = quartz

    with caplog.at_level(logging.ERROR, logger="agent-eyes"):
        assert backend.type_text(secret) is False

    assert secret not in caplog.text


def test_windows_default_sends_one_uninterrupted_unicode_batch_without_sleep(
    monkeypatch,
):
    backend = _make_windows_backend()
    text = "A😀€" * 400

    def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("default typing slept")

    monkeypatch.setattr(input_sim.time, "sleep", unexpected_sleep)

    assert backend.type_text(text) is True

    backend._send_inputs.assert_called_once()
    events = backend._send_inputs.call_args.args[0]
    units = input_sim._utf16_code_units(text)
    assert [event._input.ki.wScan for event in events] == [
        unit for unit in units for _ in (0, 1)
    ]
    assert [event._input.ki.dwFlags for event in events] == [
        flag for _ in units for flag in (0x0004, 0x0004 | 0x0002)
    ]


def test_windows_human_like_mode_batches_each_character_then_delays(monkeypatch):
    backend = _make_windows_backend()
    sleeps: list[float] = []
    monkeypatch.setattr(input_sim.time, "sleep", sleeps.append)
    monkeypatch.setattr(input_sim.random, "uniform", lambda _low, _high: 1.0)

    assert backend.type_text("A😀", delay=0.01, human_like=True) is True

    assert backend._send_inputs.call_count == 2
    assert [len(call.args[0]) for call in backend._send_inputs.call_args_list] == [2, 4]
    assert sleeps == [0.01, 0.01]


def test_windows_send_inputs_fails_on_partial_native_dispatch():
    class Input(ctypes.Structure):
        _fields_ = [("value", ctypes.c_uint)]

    backend = input_sim.WindowsInputBackend.__new__(input_sim.WindowsInputBackend)
    backend._INPUT = Input
    backend._user32 = MagicMock()
    backend._user32.SendInput.return_value = 1

    assert backend._send_inputs([Input(1), Input(2)]) is False
    backend._user32.SendInput.assert_called_once()


def test_windows_send_input_fails_when_native_dispatch_count_is_zero():
    class Input(ctypes.Structure):
        _fields_ = [("value", ctypes.c_uint)]

    backend = input_sim.WindowsInputBackend.__new__(input_sim.WindowsInputBackend)
    backend._user32 = MagicMock()
    backend._user32.SendInput.return_value = 0

    assert backend._send_input(Input(1)) is False


def test_windows_composite_input_methods_propagate_rejected_batch(monkeypatch):
    backend = input_sim.WindowsInputBackend.__new__(input_sim.WindowsInputBackend)
    backend._load = MagicMock()
    backend._INPUT = _FakeInput
    backend._KEYBDINPUT = _FakeKeyboardInput
    backend._MOUSEINPUT = MagicMock()
    backend._user32 = MagicMock()
    backend._user32.GetSystemMetrics.side_effect = lambda metric: 1920 if metric == 0 else 1080
    backend._send_inputs = MagicMock(return_value=False)
    monkeypatch.setattr(input_sim.time, "sleep", lambda _seconds: None)

    assert backend.press_key("enter") is False
    assert backend.hotkey("control", "a") is False
    assert backend.click(10, 20) is False
    assert backend.scroll(10, 20) is False
    assert backend.drag(10, 20, 30, 40) is False


def test_applescript_press_key_propagates_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        input_sim.subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(returncode=1)),
    )

    assert input_sim.AppleScriptInputBackend().press_key("enter") is False


def test_applescript_availability_requires_system_events_permission(monkeypatch):
    monkeypatch.setattr(input_sim.sys, "platform", "darwin")
    runner = MagicMock(
        return_value=SimpleNamespace(returncode=0, stdout="false\n")
    )
    monkeypatch.setattr(input_sim.subprocess, "run", runner)

    assert input_sim.AppleScriptInputBackend().is_available() is False
    assert runner.call_args.kwargs["timeout"] == 3


def test_windows_type_failure_does_not_log_typed_content(caplog):
    secret = "sentinel-secret-windows"
    backend = _make_windows_backend()
    backend._send_inputs.side_effect = RuntimeError(secret)

    with caplog.at_level(logging.ERROR, logger="agent-eyes"):
        assert backend.type_text(secret) is False

    assert secret not in caplog.text


def test_applescript_default_uses_one_bulk_command_and_stdin(monkeypatch):
    secret = "sentinel-secret-applescript 😀"
    runner = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(input_sim.subprocess, "run", runner)

    assert input_sim.AppleScriptInputBackend().type_text(secret) is True

    args, kwargs = runner.call_args
    assert secret not in repr(args)
    assert args[0] == ["osascript", "-l", "JavaScript"]
    assert "se.keystroke(text);" in kwargs["input"]
    assert "for (var i" not in kwargs["input"]
    assert json.dumps(secret) in kwargs["input"]
    assert kwargs["text"] is True


def test_applescript_human_like_mode_is_explicit(monkeypatch):
    runner = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(input_sim.subprocess, "run", runner)

    assert (
        input_sim.AppleScriptInputBackend().type_text(
            "abc",
            delay=0.01,
            human_like=True,
        )
        is True
    )

    script = runner.call_args.kwargs["input"]
    assert "for (var i" in script
    assert "delay(0.01);" in script


def test_applescript_nonzero_exit_fails_without_echoing_content(monkeypatch, caplog):
    secret = "sentinel-secret-timeout"
    runner = MagicMock(
        side_effect=subprocess.TimeoutExpired(
            ["osascript", "-l", "JavaScript", "-e", secret],
            1,
        )
    )
    monkeypatch.setattr(input_sim.subprocess, "run", runner)

    with caplog.at_level(logging.ERROR, logger="agent-eyes"):
        assert input_sim.AppleScriptInputBackend().type_text(secret) is False

    assert secret not in caplog.text


def test_applescript_nonzero_process_status_is_failure(monkeypatch):
    monkeypatch.setattr(
        input_sim.subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(returncode=1)),
    )

    assert input_sim.AppleScriptInputBackend().type_text("text") is False
