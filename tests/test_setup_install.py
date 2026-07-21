from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_eyes.setup.install import (
    apply_install_plan,
    build_install_plan,
    resolve_persistent_executable,
    select_install_manager,
)
from agent_eyes.setup import install


_LAUNCHER_NAME = "agent-eyes.exe" if sys.platform == "win32" else "agent-eyes"


@pytest.fixture(autouse=True)
def _isolate_setup_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EYES_STATE_DIR", str(tmp_path / "state"))


def test_uv_plan_is_version_pinned_and_platform_specific():
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    assert plan.command == (
        "/usr/local/bin/uv",
        "tool",
        "install",
        "agent-eyes[macos]==0.8.0",
    )
    assert plan.privileged is False
    assert "pip install" not in " ".join(plan.command)


@pytest.mark.parametrize(
    ("manager", "manager_path"),
    [("pipx", None), (None, "/usr/local/bin/pipx")],
)
def test_install_plan_rejects_partial_manager_override(manager, manager_path):
    with pytest.raises(ValueError, match="provided together"):
        build_install_plan(
            version="0.8.0",
            platform_name="darwin",
            manager=manager,
            manager_path=manager_path,
        )


def test_windows_plan_installs_declared_windows_extra():
    plan = build_install_plan(
        version="0.8.0",
        platform_name="win32",
        manager="uv",
        manager_path="C:/uv.exe",
    )

    assert plan.command[-1] == "agent-eyes[windows]==0.8.0"


def test_linux_pipx_plan_uses_system_site_packages():
    plan = build_install_plan(
        version="0.8.0",
        platform_name="linux",
        manager="pipx",
        manager_path="/usr/bin/pipx",
        python_path="/usr/bin/python3",
    )

    assert plan.command == (
        "/usr/bin/pipx",
        "install",
        "--system-site-packages",
        "--python",
        "/usr/bin/python3",
        "agent-eyes[linux]==0.8.0",
    )


def test_repair_plan_is_the_only_plan_that_forces_reinstall():
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
        force=True,
    )

    assert plan.command == (
        "/usr/local/bin/uv",
        "tool",
        "install",
        "--force",
        "agent-eyes[macos]==0.8.0",
    )


def test_linux_rejects_uv_because_it_cannot_see_system_gi():
    with pytest.raises(ValueError, match="pipx"):
        build_install_plan(
            version="0.8.0",
            platform_name="linux",
            manager="uv",
            manager_path="/usr/local/bin/uv",
        )


def test_linux_manager_selection_requires_pipx(monkeypatch):
    monkeypatch.setattr(
        "agent_eyes.setup.install.shutil.which",
        lambda name: None if name == "pipx" else "/usr/local/bin/uv",
    )

    with pytest.raises(RuntimeError, match="pipx"):
        select_install_manager("linux")


def test_manager_specific_launcher_resolution_does_not_prefer_other_manager(
    tmp_path, monkeypatch
):
    pipx_bin = tmp_path / "pipx-bin"
    pipx_bin.mkdir()
    expected = pipx_bin / _LAUNCHER_NAME
    expected.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PIPX_BIN_DIR", str(pipx_bin))

    resolved = resolve_persistent_executable("pipx", "/usr/bin/pipx")

    assert resolved == expected


def test_install_plan_never_runs_before_consent():
    calls: list[tuple[str, ...]] = []
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: False,
        runner=lambda command: calls.append(tuple(command)),
    )

    assert result.cancelled is True
    assert result.applied is False
    assert calls == []


def test_dry_run_is_side_effect_free():
    calls: list[tuple[str, ...]] = []
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=lambda command: calls.append(tuple(command)),
        dry_run=True,
    )

    assert result.applied is False
    assert result.dry_run is True
    assert calls == []


def test_approved_install_uses_argument_array():
    calls: list[tuple[str, ...]] = []
    installed = {"version": None}
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    def runner(command):
        calls.append(tuple(command))
        installed["version"] = plan.version

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=runner,
        installed_version=lambda _plan: installed["version"],
        launcher_is_usable=lambda _plan: True,
    )

    assert result.applied is True
    assert calls == [plan.command]


def test_default_install_runner_has_a_bounded_deadline(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install.subprocess, "run", run)

    install._default_runner(("uv", "tool", "install", "agent-eyes"))

    assert observed["timeout"] == install.INSTALL_TIMEOUT_SECONDS
    assert observed["timeout"] > 0
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["env"]["PIP_NO_INPUT"] == "1"
    assert observed["env"]["UV_NO_PROGRESS"] == "1"


def test_install_reports_failure_when_manager_metadata_does_not_confirm_version(
    tmp_path,
):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=lambda _command: None,
        installed_version=lambda _plan: None,
        lock_path=tmp_path / ".setup.lock",
    )

    assert result.applied is False
    assert result.error is not None
    assert "verification" in result.error.lower()


def test_already_current_install_requires_a_usable_launcher(tmp_path):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=lambda _command: pytest.fail("invalid current install must not run silently"),
        installed_version=lambda _plan: plan.version,
        launcher_is_usable=lambda _plan: False,
        lock_path=tmp_path / ".setup.lock",
    )

    assert result.applied is False
    assert result.already_current is False
    assert result.error is not None
    assert "launcher" in result.error.lower()


@pytest.mark.parametrize("stdout", ["", "relative/bin\n"])
def test_uv_launcher_resolution_rejects_invalid_bin_directory(stdout):
    observed = {}

    def runner(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with pytest.raises(RuntimeError, match="absolute bin directory"):
        resolve_persistent_executable("uv", "/usr/local/bin/uv", runner=runner)

    assert observed["timeout"] > 0
    assert observed["stdin"] is subprocess.DEVNULL


def test_uv_installed_version_probe_reads_manager_metadata(monkeypatch):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="other v1.0.0\nagent-eyes v0.8.0\n", stderr=""
        ),
    )

    assert install._installed_version(plan) == "0.8.0"


def test_pipx_installed_version_probe_reads_manager_metadata(monkeypatch):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="pipx",
        manager_path="/usr/local/bin/pipx",
    )
    payload = (
        '{"venvs":{"agent-eyes":{"metadata":{"main_package":'
        '{"package_version":"0.8.0"}}}}}'
    )
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=payload, stderr=""
        ),
    )

    assert install._installed_version(plan) == "0.8.0"


def test_force_plan_runs_even_when_the_same_version_is_installed(tmp_path):
    calls = []
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
        force=True,
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=lambda command: calls.append(tuple(command)),
        installed_version=lambda _: "0.8.0",
        launcher_is_usable=lambda _plan: True,
        lock_path=tmp_path / ".setup.lock",
    )

    assert result.applied is True
    assert result.already_current is False
    assert calls == [plan.command]


def test_install_failure_does_not_echo_package_manager_output(tmp_path):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    def fail(_command):
        raise subprocess.CalledProcessError(
            1,
            plan.command,
            stderr="SENTINEL_SECRET_INDEX_CREDENTIAL",
        )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=fail,
        installed_version=lambda _: None,
        lock_path=tmp_path / ".setup.lock",
    )

    assert result.error is not None
    assert "SENTINEL_SECRET_INDEX_CREDENTIAL" not in result.error


def test_install_returns_structured_error_when_setup_lock_is_unsafe(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("keep")
    lock_path = tmp_path / ".setup.lock"
    lock_path.symlink_to(target)
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: True,
        runner=lambda _command: pytest.fail("unsafe lock must prevent installation"),
        installed_version=lambda _: None,
        lock_path=lock_path,
    )

    assert result.applied is False
    assert result.error is not None
    assert "setup lock" in result.error.lower()
    assert target.read_text() == "keep"


@pytest.mark.parametrize("mode", ["dry_run", "declined"])
def test_preview_and_decline_do_not_create_the_setup_lock(tmp_path, mode):
    lock_path = tmp_path / "state" / ".setup.lock"
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )

    result = apply_install_plan(
        plan,
        consent=lambda _: mode != "declined",
        runner=lambda _command: pytest.fail("install runner must not execute"),
        dry_run=mode == "dry_run",
        lock_path=lock_path,
    )

    assert result.applied is False
    assert not lock_path.exists()


def test_concurrent_install_is_serialized_and_skips_an_already_current_version(tmp_path):
    plan = build_install_plan(
        version="0.8.0",
        platform_name="darwin",
        manager="uv",
        manager_path="/usr/local/bin/uv",
    )
    started = threading.Event()
    release = threading.Event()
    state = {"version": None}
    calls: list[tuple[str, ...]] = []

    def installed_version(_plan):
        return state["version"]

    def runner(command):
        calls.append(tuple(command))
        started.set()
        assert release.wait(timeout=5)
        state["version"] = "0.8.0"

    def apply():
        return apply_install_plan(
            plan,
            consent=lambda _: True,
            runner=runner,
            installed_version=installed_version,
            launcher_is_usable=lambda _plan: True,
            lock_path=tmp_path / ".setup.lock",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(apply)
        assert started.wait(timeout=5)
        second = executor.submit(apply)
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert calls == [plan.command]
    assert all(result.successful for result in results)
    assert sum(result.already_current for result in results) == 1
