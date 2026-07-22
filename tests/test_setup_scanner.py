from __future__ import annotations

from pathlib import Path

from agent_eyes.setup import scanner


def _definition(tool_id: str) -> dict:
    return next(
        definition
        for definition in scanner._ai_tool_definitions()
        if definition["id"] == tool_id
    )


def test_windows_paths_honor_appdata(monkeypatch, tmp_path):
    home = tmp_path / "home"
    appdata = tmp_path / "roaming"
    monkeypatch.setattr(scanner, "_home", lambda: home)
    monkeypatch.setattr(scanner.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))

    vscode = _definition("vscode")
    claude_desktop = _definition("claude-desktop")
    zed = _definition("zed")

    assert vscode["detection_paths"] == [appdata / "Code" / "User"]
    assert claude_desktop["detection_paths"] == [appdata / "Claude"]
    assert zed["detection_paths"] == [appdata / "Zed"]


def test_windows_paths_fall_back_to_home_appdata(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(scanner, "_home", lambda: home)
    monkeypatch.setattr(scanner.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    assert _definition("vscode")["detection_paths"] == [
        home / "AppData" / "Roaming" / "Code" / "User"
    ]


def test_linux_paths_honor_xdg_config_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    monkeypatch.setattr(scanner, "_home", lambda: home)
    monkeypatch.setattr(scanner.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    vscode = _definition("vscode")
    claude_desktop = _definition("claude-desktop")
    zed = _definition("zed")

    assert vscode["detection_paths"] == [config_home / "Code" / "User"]
    assert claude_desktop["detection_paths"] == [config_home / "Claude"]
    assert zed["detection_paths"] == [config_home / "zed"]


def test_linux_paths_fall_back_to_dot_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(scanner, "_home", lambda: home)
    monkeypatch.setattr(scanner.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert _definition("vscode")["detection_paths"] == [
        home / ".config" / "Code" / "User"
    ]


def test_codex_definition_exposes_toml_and_skill_locations(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(scanner, "_home", lambda: home)

    codex = _definition("codex")

    assert codex == {
        "id": "codex",
        "name": "Codex",
        "detection_paths": [home / ".codex"],
        "config_locations": {
            "global_mcp": {
                "path": home / ".codex" / "config.toml",
                "key": "mcp_servers",
                "format": "toml",
            },
            "project_mcp": {
                "path": Path(".codex") / "config.toml",
                "key": "mcp_servers",
                "format": "toml",
            },
            "skills": {
                "path": home / ".codex" / "skills",
                "type": "directory",
            },
        },
        "supports_skills": True,
        "supports_agents": False,
    }


def test_scan_detects_codex_without_writing_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    monkeypatch.setattr(scanner, "_home", lambda: home)
    monkeypatch.setattr(scanner.sys, "platform", "darwin")
    before = set(home.rglob("*"))

    detected = scanner.scan_ai_tools()

    assert [tool["id"] for tool in detected] == ["codex"]
    assert detected[0]["supports_skills"] is True
    assert detected[0]["supports_agents"] is False
    assert set(home.rglob("*")) == before
    assert not (codex_home / "config.toml").exists()
