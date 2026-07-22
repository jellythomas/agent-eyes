from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_eyes import __version__


_LAUNCHER_NAME = "agent-eyes.exe" if sys.platform == "win32" else "agent-eyes"


def test_cli_import_does_not_import_server():
    sys.modules.pop("agent_eyes.cli", None)
    sys.modules.pop("agent_eyes.server", None)

    importlib.import_module("agent_eyes.cli")

    assert "agent_eyes.server" not in sys.modules


def test_parser_exposes_guided_setup_commands():
    from agent_eyes.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["doctor", "--json"]).json is True
    assert parser.parse_args(["install", "--dry-run"]).dry_run is True
    assert parser.parse_args(["init", "--client", "cursor"]).client == ["cursor"]
    assert parser.parse_args(["setup", "--yes"]).yes is True


def test_compatibility_flags_have_truthful_help_and_remain_accepted(capsys):
    from agent_eyes.cli import build_parser

    parser = build_parser()

    with pytest.raises(SystemExit) as doctor_exit:
        parser.parse_args(["doctor", "--help"])
    assert doctor_exit.value.code == 0
    doctor_help = " ".join(capsys.readouterr().out.split())
    assert "doctor always performs live checks" in doctor_help
    assert parser.parse_args(["doctor", "--refresh"]).refresh is True

    with pytest.raises(SystemExit) as install_exit:
        parser.parse_args(["install", "--help"])
    assert install_exit.value.code == 0
    install_help = " ".join(capsys.readouterr().out.split())
    assert "does not change the platform package installed" in install_help
    assert parser.parse_args(["install", "--profile", "full"]).profile == "full"

    for command in ("init", "setup"):
        with pytest.raises(SystemExit) as client_exit:
            parser.parse_args([command, "--help"])
        assert client_exit.value.code == 0
        client_help = " ".join(capsys.readouterr().out.split())
        assert "Explicitly select the default set of detected MCP clients" in client_help
        assert parser.parse_args([command, "--all-detected"]).all_detected is True


@pytest.mark.parametrize("command", ("init", "setup"))
def test_unknown_client_is_usage_error(
    command,
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_eyes import cli
    from agent_eyes.setup import readiness

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    report = SimpleNamespace(status=readiness.ReadinessStatus.READY)
    monkeypatch.setattr(cli, "_persistent_executable", lambda: launcher)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)
    monkeypatch.setattr(cli, "_prepare_install", lambda repair: (launcher, None))
    monkeypatch.setattr(cli, "_probe_persistent_readiness", lambda *_args: report)

    result = cli.main(
        [command, "--client", "unknown", "--dry-run", "--json"]
    )

    assert result == int(cli.ExitCode.USAGE)
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": "Unknown MCP client(s): unknown",
        "status": "error",
    }


def test_setup_environment_value_error_is_setup_required(monkeypatch, capsys):
    from agent_eyes import cli

    monkeypatch.setattr(
        cli,
        "_prepare_install",
        lambda repair: (_ for _ in ()).throw(ValueError("Unsupported platform: plan9")),
    )

    result = cli.main(["setup", "--dry-run", "--json"])

    assert result == int(cli.ExitCode.SETUP_REQUIRED)
    assert json.loads(capsys.readouterr().out) == {
        "error": "Unsupported platform: plan9",
        "status": "error",
    }


def test_no_arguments_remain_serve_alias(monkeypatch):
    from agent_eyes import cli

    calls: list[str] = []
    monkeypatch.setattr(cli, "_run_serve", lambda args: calls.append(args.command) or 0)

    assert cli.main([]) == 0
    assert calls == ["serve"]


def test_version_flag_is_lightweight(capsys):
    from agent_eyes.cli import main

    with pytest_raises_system_exit(0):
        main(["--version"])

    assert __version__ in capsys.readouterr().out


class pytest_raises_system_exit:
    def __init__(self, code: int):
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        assert exception_type is SystemExit
        assert exception.code == self.code
        return True


def test_persistent_executable_keeps_stable_shim_path(tmp_path, monkeypatch):
    from agent_eyes import cli

    bin_dir = tmp_path / "bin"
    tool_dir = tmp_path / "tools" / "agent-eyes" / "bin"
    bin_dir.mkdir()
    tool_dir.mkdir(parents=True)
    target = tool_dir / _LAUNCHER_NAME
    target.write_text("#!/bin/sh\n")
    shim = bin_dir / _LAUNCHER_NAME
    shim.symlink_to(target)

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, f"{bin_dir}\n", ""),
    )

    assert cli._persistent_executable() == shim


def test_persistent_executable_rejects_active_virtualenv_path_launcher(
    tmp_path, monkeypatch
):
    from agent_eyes import cli

    environment = tmp_path / ".venv"
    launcher = (
        environment
        / ("Scripts" if sys.platform == "win32" else "bin")
        / _LAUNCHER_NAME
    )
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher")
    monkeypatch.setenv("PIPX_BIN_DIR", str(tmp_path / "pipx-bin"))
    monkeypatch.setattr(cli.sys, "prefix", str(environment))
    monkeypatch.setattr(cli.sys, "base_prefix", str(tmp_path / "python"))
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: str(launcher) if name == "agent-eyes" else None,
    )
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)

    assert cli._persistent_executable() is None


def test_persistent_executable_keeps_manager_launcher_inside_active_environment(
    tmp_path, monkeypatch
):
    from agent_eyes import cli

    environment = tmp_path / "tool-environment"
    bin_dir = environment / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / _LAUNCHER_NAME
    launcher.write_text("launcher")
    monkeypatch.setattr(cli.sys, "prefix", str(environment))
    monkeypatch.setattr(cli.sys, "base_prefix", str(tmp_path / "python"))
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"{bin_dir}\n", ""
        ),
    )
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)

    assert cli._persistent_executable() == launcher


def test_persistent_executable_prefers_current_pipx_over_stale_uv(
    tmp_path, monkeypatch
):
    from agent_eyes import cli

    uv_bin = tmp_path / "uv-bin"
    pipx_bin = tmp_path / "pipx-bin"
    uv_bin.mkdir()
    pipx_bin.mkdir()
    stale = uv_bin / _LAUNCHER_NAME
    current = pipx_bin / _LAUNCHER_NAME
    stale.write_text("stale")
    current.write_text("current")
    monkeypatch.setenv("PIPX_BIN_DIR", str(pipx_bin))
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"{uv_bin}\n", ""
        ),
    )
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: path == current)

    assert cli._persistent_executable() == current


def test_install_json_skips_package_manager_when_launcher_is_current(
    tmp_path, monkeypatch, capsys
):
    from agent_eyes import cli
    from agent_eyes.setup import install

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    monkeypatch.setattr(cli, "_persistent_executable", lambda: launcher)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: path == launcher)
    monkeypatch.setattr(
        install,
        "build_install_plan",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )

    assert cli.main(["install", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "already_current"
    assert payload["executable"] == str(launcher)
    assert payload["install"] is None


def test_repair_forces_install_and_resolves_that_managers_launcher(
    tmp_path, monkeypatch, capsys
):
    from agent_eyes import cli
    from agent_eyes.setup import install

    old_launcher = tmp_path / "old" / "agent-eyes"
    expected = tmp_path / "uv-bin" / "agent-eyes"
    expected.parent.mkdir()
    expected.write_text("launcher")
    seen: dict[str, object] = {}

    monkeypatch.setattr(cli, "_persistent_executable", lambda: old_launcher)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: path == expected)

    def build(**kwargs):
        seen["force"] = kwargs["force"]
        return install.InstallPlan("uv", ("/opt/uv", "tool", "install"), "install")

    monkeypatch.setattr(install, "build_install_plan", build)
    monkeypatch.setattr(
        install,
        "apply_install_plan",
        lambda plan, **kwargs: install.InstallResult(applied=True),
    )

    def resolve(manager, manager_path):
        seen["resolved"] = (manager, manager_path)
        return expected

    monkeypatch.setattr(install, "resolve_persistent_executable", resolve)

    assert cli.main(["install", "--repair", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert seen == {"force": True, "resolved": ("uv", "/opt/uv")}
    assert payload["status"] == "installed"
    assert payload["executable"] == str(expected)


def test_persistent_readiness_parse_error_does_not_echo_launcher_output(
    tmp_path,
    monkeypatch,
):
    from agent_eyes import cli

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o700)
    secret = "SENTINEL_PROVIDER_SECRET_OUTPUT"
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            2,
            stdout="not-json-" + secret,
            stderr=secret,
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        cli._probe_persistent_readiness(launcher, "standard")

    assert secret not in str(exc_info.value)
    assert "stdout_chars=" in str(exc_info.value)


def test_persistent_readiness_probe_isolates_launcher_diagnostic_state(
    tmp_path,
    monkeypatch,
):
    from agent_eyes import cli

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o700)
    persistent_state = tmp_path / "persistent-state"
    monkeypatch.setenv("AGENT_EYES_STATE_DIR", str(persistent_state))
    observed: dict[str, Path] = {}

    def run(*args, **kwargs):
        isolated_state = Path(kwargs["env"]["AGENT_EYES_STATE_DIR"])
        observed["state"] = isolated_state
        assert isolated_state != persistent_state
        assert isolated_state.is_dir()
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(
                {
                    "status": "ready",
                    "capabilities": [],
                    "fingerprint": {},
                    "checked_at": "2026-07-22T00:00:00+00:00",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", run)

    report = cli._probe_persistent_readiness(launcher, "standard")

    assert report.status.value == "ready"
    assert not persistent_state.exists()
    assert not observed["state"].exists()


def test_client_configs_are_all_preflighted_before_any_write(tmp_path, monkeypatch):
    from agent_eyes import cli
    from agent_eyes.setup import configurator

    executable = tmp_path / "agent-eyes"
    events: list[str] = []
    items = [
        {"client": "first", "path": str(tmp_path / "first.json"), "servers_key": "mcpServers", "is_zed": False},
        {"client": "second", "path": str(tmp_path / "second.json"), "servers_key": "servers", "is_zed": False},
    ]

    def preflight(path, **kwargs):
        events.append(f"preflight:{path.name}")
        return configurator.ConfigurePlan(
            changed=True,
            path=str(path),
            write_path=str(path),
            original_content=None,
            rendered_content="{}\n",
            source_format="json",
        )

    def apply(plans):
        events.append("apply-group:" + ",".join(Path(plan.path).name for plan in plans))
        return tuple(
            configurator.ConfigureResult(
                changed=True,
                applied=True,
                path=plan.path,
            )
            for plan in plans
        )

    monkeypatch.setattr(configurator, "preflight_mcp_file", preflight)
    monkeypatch.setattr(configurator, "apply_mcp_plans", apply)

    cli._apply_client_configs(items, executable, dry_run=False)

    assert events == [
        "preflight:first.json",
        "preflight:second.json",
        "apply-group:first.json,second.json",
    ]


def test_client_plan_includes_codex_toml_and_canonical_skill_artifacts(tmp_path):
    from agent_eyes import cli

    target = {
        "id": "codex",
        "config_locations": {
            "global_mcp": {
                "path": tmp_path / "config.toml",
                "key": "mcp_servers",
                "format": "toml",
            },
            "skills": {
                "path": tmp_path / "skills",
                "type": "directory",
            },
        },
        "supports_skills": True,
    }

    assert cli._client_change_plan([target]) == [
        {
            "artifact": "mcp",
            "client": "codex",
            "path": str(tmp_path / "config.toml"),
            "servers_key": "mcp_servers",
            "is_zed": False,
            "format": "toml",
        },
        {
            "artifact": "skill",
            "client": "codex",
            "path": str(tmp_path / "skills" / "agent-eyes" / "SKILL.md"),
        },
        {
            "artifact": "skill-metadata",
            "client": "codex",
            "path": str(
                tmp_path
                / "skills"
                / "agent-eyes"
                / "agents"
                / "openai.yaml"
            ),
        },
    ]


def test_codex_config_and_skills_apply_in_one_transaction(tmp_path, monkeypatch):
    from agent_eyes import cli
    from agent_eyes.setup import configurator
    from agent_eyes.setup.templates.openai_skill import OPENAI_YAML
    from agent_eyes.setup.templates.skill import SKILL_MD

    executable = tmp_path / "agent-eyes"
    target = {
        "id": "codex",
        "config_locations": {
            "global_mcp": {
                "path": tmp_path / "config.toml",
                "key": "mcp_servers",
                "format": "toml",
            },
            "skills": {"path": tmp_path / "skills", "type": "directory"},
        },
        "supports_skills": True,
    }
    seen: list[tuple[str, ...]] = []
    real_apply = configurator.apply_mcp_plans

    def apply(plans):
        seen.append(tuple(plan.source_format for plan in plans))
        return real_apply(plans, lock_path=tmp_path / "setup.lock")

    monkeypatch.setattr(configurator, "apply_mcp_plans", apply)
    plan = cli._client_change_plan([target])
    prepared = cli._preflight_client_configs(plan, executable)

    changes = cli._config_results(prepared, apply=True)

    assert seen == [("toml", "skill", "skill-metadata")]
    assert all(item["applied"] for item in changes)
    configured = configurator._toml.loads(
        (tmp_path / "config.toml").read_text(encoding="utf-8")
    )
    assert configured["mcp_servers"]["agent-eyes"]["command"] == str(executable)
    assert (
        tmp_path / "skills" / "agent-eyes" / "SKILL.md"
    ).read_text(encoding="utf-8") == SKILL_MD
    assert (
        tmp_path / "skills" / "agent-eyes" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8") == OPENAI_YAML


def test_skill_aware_dry_run_writes_nothing(tmp_path):
    from agent_eyes import cli

    executable = tmp_path / "agent-eyes"
    target = {
        "id": "claude-code",
        "config_locations": {
            "global_mcp": {
                "path": tmp_path / "claude.json",
                "key": "mcpServers",
                "format": "json",
            },
            "skills": {"path": tmp_path / "skills", "type": "directory"},
        },
        "supports_skills": True,
    }
    plan = cli._client_change_plan([target])

    changes = cli._apply_client_configs(plan, executable, dry_run=True)

    assert all(item["changed"] and not item["applied"] for item in changes)
    assert not (tmp_path / "claude.json").exists()
    assert not (tmp_path / "skills").exists()


def test_malformed_later_client_leaves_earlier_config_untouched(tmp_path):
    from agent_eyes import cli
    from agent_eyes.setup.configurator import InvalidConfigError

    executable = tmp_path / "agent-eyes"
    first = tmp_path / "first.json"
    first_content = json.dumps({"mcpServers": {}})
    first.write_text(first_content)
    second = tmp_path / "second.json"
    second_content = "{malformed user config"
    second.write_text(second_content)
    items = [
        {
            "client": "first",
            "path": str(first),
            "servers_key": "mcpServers",
            "is_zed": False,
        },
        {
            "client": "second",
            "path": str(second),
            "servers_key": "mcpServers",
            "is_zed": False,
        },
    ]

    with pytest.raises(InvalidConfigError):
        cli._apply_client_configs(items, executable, dry_run=False)

    assert first.read_text() == first_content
    assert second.read_text() == second_content


def test_init_json_emits_one_document_after_apply(tmp_path, monkeypatch, capsys):
    from agent_eyes import cli
    from agent_eyes.setup import state

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    monkeypatch.setattr(cli, "_persistent_executable", lambda: launcher)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(cli, "_apply_client_configs", lambda *args, **kwargs: [])
    monkeypatch.setattr(state, "mark_initialized", lambda *args: None)

    assert cli.main(["init", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "configured"
    assert payload["changes"] == []


def test_setup_skips_healthy_install_and_emits_one_json_document(
    tmp_path, monkeypatch, capsys
):
    from agent_eyes import cli
    from agent_eyes.setup import install, readiness, state

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    report = SimpleNamespace(
        status=readiness.ReadinessStatus.READY,
        to_dict=lambda: {"status": "ready"},
        to_text=lambda verbose=False: "ready",
    )
    monkeypatch.setattr(cli, "_persistent_executable", lambda: launcher)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(cli, "_apply_client_configs", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        install,
        "build_install_plan",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )
    monkeypatch.setattr(readiness, "probe_current_readiness", lambda **kwargs: report)
    monkeypatch.setattr(
        cli,
        "_probe_persistent_readiness",
        lambda executable, profile: report,
    )
    monkeypatch.setattr(readiness.ReadinessStore, "save", lambda self, value: None)
    monkeypatch.setattr(state, "mark_initialized", lambda *args: None)

    assert cli.main(["setup", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "ready"
    assert payload["install"] is None
    assert payload["executable"] == str(launcher)


def test_setup_uses_selected_managers_launcher_after_install(
    tmp_path, monkeypatch, capsys
):
    from agent_eyes import cli
    from agent_eyes.setup import install, readiness, state

    expected = tmp_path / "pipx-bin" / "agent-eyes"
    expected.parent.mkdir()
    install_plan = install.InstallPlan(
        "pipx",
        ("/opt/pipx", "install", "agent-eyes"),
        "install",
    )
    report = SimpleNamespace(
        status=readiness.ReadinessStatus.READY,
        to_dict=lambda: {"status": "ready"},
        to_text=lambda verbose=False: "ready",
    )
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_persistent_executable", lambda: None)
    monkeypatch.setattr(
        cli,
        "_launcher_matches_current",
        lambda path: path == expected and expected.exists(),
    )
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(cli, "_apply_client_configs", lambda *args, **kwargs: [])
    monkeypatch.setattr(install, "build_install_plan", lambda **kwargs: install_plan)

    def apply_install(plan, **kwargs):
        expected.write_text("launcher")
        return install.InstallResult(applied=True)

    monkeypatch.setattr(install, "apply_install_plan", apply_install)

    def resolve(manager, manager_path):
        seen.append((manager, manager_path))
        return expected

    monkeypatch.setattr(install, "resolve_persistent_executable", resolve)
    monkeypatch.setattr(readiness, "probe_current_readiness", lambda **kwargs: report)
    monkeypatch.setattr(
        cli,
        "_probe_persistent_readiness",
        lambda executable, profile: report,
    )
    monkeypatch.setattr(readiness.ReadinessStore, "save", lambda self, value: None)
    monkeypatch.setattr(state, "mark_initialized", lambda *args: None)

    assert cli.main(["setup", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert seen == [("pipx", "/opt/pipx")]
    assert payload["status"] == "ready"
    assert payload["executable"] == str(expected)


def test_setup_repairs_current_launcher_when_live_dependencies_are_missing(
    tmp_path, monkeypatch, capsys
):
    from agent_eyes import cli
    from agent_eyes.setup import install, readiness, state

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    install_plan = install.InstallPlan(
        "uv",
        ("/opt/uv", "tool", "install", "--force", "agent-eyes"),
        "repair",
    )
    reports = iter(
        [
            SimpleNamespace(
                status=readiness.ReadinessStatus.SETUP_REQUIRED,
                to_dict=lambda: {"status": "setup_required"},
                to_text=lambda verbose=False: "setup_required",
            ),
            SimpleNamespace(
                status=readiness.ReadinessStatus.READY,
                to_dict=lambda: {"status": "ready"},
                to_text=lambda verbose=False: "ready",
            ),
        ]
    )
    prepare_calls: list[bool] = []

    def prepare(*, repair):
        prepare_calls.append(repair)
        return (launcher, install_plan if repair else None)

    monkeypatch.setattr(cli, "_prepare_install", prepare)
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(
        install,
        "apply_install_plan",
        lambda plan, **kwargs: install.InstallResult(applied=True),
    )
    monkeypatch.setattr(
        readiness,
        "probe_current_readiness",
        lambda **kwargs: next(reports),
    )
    monkeypatch.setattr(
        cli,
        "_probe_persistent_readiness",
        lambda executable, profile: next(reports),
    )
    monkeypatch.setattr(readiness.ReadinessStore, "save", lambda self, value: None)
    monkeypatch.setattr(state, "mark_initialized", lambda *args: None)

    assert cli.main(["setup", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert prepare_calls == [False, True]
    assert payload["precheck"]["status"] == "setup_required"
    assert payload["install"]["plan"]["description"] == "repair"


def test_setup_health_checks_same_version_persistent_launcher_before_reuse(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_eyes import cli
    from agent_eyes.setup import install, readiness, state

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    repair_plan = install.InstallPlan(
        "uv",
        ("/opt/uv", "tool", "install", "--force", "agent-eyes"),
        "repair exact launcher",
    )
    broken = SimpleNamespace(
        status=readiness.ReadinessStatus.SETUP_REQUIRED,
        core_ready=False,
        to_dict=lambda: {"status": "setup_required", "core_ready": False},
        to_text=lambda verbose=False: "setup_required",
    )
    ready = SimpleNamespace(
        status=readiness.ReadinessStatus.READY,
        core_ready=True,
        to_dict=lambda: {"status": "ready", "core_ready": True},
        to_text=lambda verbose=False: "ready",
    )
    reports = iter([broken, ready])
    prepare_calls: list[bool] = []

    def prepare(*, repair):
        prepare_calls.append(repair)
        return launcher, repair_plan if repair else None

    monkeypatch.setattr(cli, "_prepare_install", prepare)
    monkeypatch.setattr(
        cli,
        "_probe_persistent_readiness",
        lambda executable, profile: next(reports),
    )
    monkeypatch.setattr(
        readiness,
        "probe_current_readiness",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap environment must not approve launcher reuse")
        ),
    )
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(cli, "_launcher_matches_current", lambda path: True)
    monkeypatch.setattr(
        install,
        "apply_install_plan",
        lambda plan, **kwargs: install.InstallResult(applied=True),
    )
    monkeypatch.setattr(readiness.ReadinessStore, "save", lambda self, value: None)
    marked = MagicMock()
    monkeypatch.setattr(state, "mark_initialized", marked)

    assert cli.main(["setup", "--json", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert prepare_calls == [False, True]
    assert payload["precheck"]["status"] == "setup_required"
    assert payload["status"] == "ready"
    marked.assert_called_once()


def test_setup_does_not_mark_initialized_when_core_is_not_ready(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_eyes import cli
    from agent_eyes.setup import readiness, state

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("launcher")
    report = SimpleNamespace(
        status=readiness.ReadinessStatus.PERMISSION_REQUIRED,
        core_ready=False,
        to_dict=lambda: {"status": "permission_required", "core_ready": False},
        to_text=lambda verbose=False: "permission_required",
    )
    monkeypatch.setattr(cli, "_prepare_install", lambda repair: (launcher, None))
    monkeypatch.setattr(cli, "_probe_persistent_readiness", lambda *_args: report)
    monkeypatch.setattr(cli, "_client_targets", lambda clients: [])
    monkeypatch.setattr(readiness.ReadinessStore, "save", lambda self, value: None)
    marked = MagicMock()
    monkeypatch.setattr(state, "mark_initialized", marked)

    assert cli.main(["setup", "--json", "--yes"]) == int(cli.ExitCode.ACTION_REQUIRED)
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "permission_required"
    assert payload["install"] is None
    marked.assert_not_called()
