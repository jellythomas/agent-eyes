from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_open_url_uses_the_users_default_browser_first(monkeypatch):
    import webbrowser

    from agent_eyes.platform_utils import open_url_in_browser

    calls = []
    monkeypatch.setattr(
        webbrowser,
        "open_new_tab",
        lambda url: calls.append(url) or True,
    )
    monkeypatch.setattr(
        webbrowser,
        "get",
        lambda name: (_ for _ in ()).throw(AssertionError(f"forced browser: {name}")),
    )

    success, message = open_url_in_browser("https://example.com")

    assert success is True
    assert calls == ["https://example.com"]
    assert "default browser" in message


@pytest.mark.parametrize(
    "process_name",
    [
        "/Applications/Safari.app/Contents/MacOS/Safari",
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "msedge.exe",
        "brave-browser",
        "zen-browser",
    ],
)
def test_browser_pid_detection_is_not_chromium_only(monkeypatch, process_name):
    from agent_eyes import platform_utils

    monkeypatch.setattr(platform_utils, "get_process_name", lambda pid: process_name)

    assert platform_utils.is_browser_pid(123) is True


def test_browser_pid_detection_rejects_unrelated_process(monkeypatch):
    from agent_eyes import platform_utils

    monkeypatch.setattr(platform_utils, "get_process_name", lambda pid: "Visual Studio Code")

    assert platform_utils.is_browser_pid(123) is False


def test_windows_process_names_use_one_fresh_bulk_snapshot(monkeypatch):
    from agent_eyes import platform_utils

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '"chrome.EXE","42","Console","1","123,456 K"\n'
                '"firefox.exe","73","Console","1","80,000 K"\n'
                '"notepad.exe","99","Console","1","1,000 K"\n'
            ),
        )

    monkeypatch.setattr(platform_utils.sys, "platform", "win32")
    monkeypatch.setattr(platform_utils.subprocess, "run", fake_run)

    names = platform_utils.get_process_names([42, 73, 73, 0, -1, True])

    assert names == {42: "chrome", 73: "firefox"}
    assert calls == [
        (
            ["tasklist", "/FO", "CSV", "/NH"],
            {"capture_output": True, "text": True, "timeout": 2},
        )
    ]


def test_open_url_failure_does_not_recommend_one_browser(monkeypatch):
    import webbrowser

    from agent_eyes.platform_utils import open_url_in_browser

    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: False)

    success, message = open_url_in_browser("https://example.com")

    assert success is False
    assert "Chrome" not in message
