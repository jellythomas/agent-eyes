"""Explicit, consent-gated persistent installation planning."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .state import setup_process_lock


INSTALL_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class InstallPlan:
    manager: str
    command: tuple[str, ...]
    description: str
    privileged: bool = False
    version: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "command": list(self.command),
            "description": self.description,
            "privileged": self.privileged,
            "version": self.version,
        }


@dataclass(frozen=True)
class InstallResult:
    applied: bool
    cancelled: bool = False
    dry_run: bool = False
    error: str | None = None
    already_current: bool = False

    @property
    def successful(self) -> bool:
        return self.applied and self.error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "cancelled": self.cancelled,
            "dry_run": self.dry_run,
            "already_current": self.already_current,
            "error": self.error,
        }


def _platform_extra(platform_name: str) -> str:
    mapping = {
        "darwin": "macos",
        "win32": "windows",
        "linux": "linux",
    }
    try:
        return mapping[platform_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {platform_name}") from exc


def select_install_manager(platform_name: str | None = None) -> tuple[str, str]:
    """Select a persistent isolated-tool manager without installing one."""

    current_platform = platform_name or sys.platform
    if current_platform == "linux":
        pipx_path = shutil.which("pipx")
        if pipx_path:
            return "pipx", pipx_path
        raise RuntimeError(
            "Linux requires pipx so the isolated tool can access distro-provided "
            "AT-SPI/PyGObject through --system-site-packages. Install pipx, then rerun "
            "agent-eyes setup."
        )

    order = ("uv", "pipx")
    for manager in order:
        manager_path = shutil.which(manager)
        if manager_path:
            return manager, manager_path
    raise RuntimeError(
        "Neither uv nor pipx is available. Install uv, then rerun agent-eyes setup."
    )


def build_install_plan(
    *,
    version: str,
    platform_name: str | None = None,
    manager: str | None = None,
    manager_path: str | None = None,
    python_path: str | None = None,
    force: bool = False,
) -> InstallPlan:
    """Build an exact persistent install command for the current platform."""

    current_platform = platform_name or sys.platform
    if (manager is None) != (manager_path is None):
        raise ValueError("manager and manager_path must be provided together")
    selected_manager, selected_path = (
        (manager, manager_path)
        if manager is not None and manager_path is not None
        else select_install_manager(current_platform)
    )
    if selected_manager is None or selected_path is None:
        raise ValueError("manager and manager_path must be provided together")

    extra = _platform_extra(current_platform)
    package = f"agent-eyes[{extra}]=={version}"
    if selected_manager == "uv":
        if current_platform == "linux":
            raise ValueError(
                "Linux installation requires pipx with --system-site-packages so "
                "the AT-SPI/PyGObject provider remains available."
            )
        command_parts = [selected_path, "tool", "install"]
        if force:
            command_parts.append("--force")
        command_parts.append(package)
        command = tuple(command_parts)
    elif selected_manager == "pipx":
        command_parts = [selected_path, "install"]
        if force:
            command_parts.append("--force")
        if current_platform == "linux":
            command_parts.append("--system-site-packages")
            command_parts.extend(("--python", python_path or "/usr/bin/python3"))
        command_parts.append(package)
        command = tuple(command_parts)
    else:
        raise ValueError(f"Unsupported install manager: {selected_manager}")

    return InstallPlan(
        manager=selected_manager,
        command=command,
        description=(
            f"Install Agent Eyes {version} with the {extra} provider "
            f"into a persistent {selected_manager} tool environment"
        ),
        version=version,
    )


def _default_runner(command: Sequence[str]) -> None:
    environment = os.environ.copy()
    environment.update({
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "UV_NO_PROGRESS": "1",
    })
    subprocess.run(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        env=environment,
    )


def _installed_version(plan: InstallPlan) -> str | None:
    """Read installed manager metadata without invoking the Agent Eyes launcher."""
    try:
        if plan.manager == "uv":
            completed = subprocess.run(
                [plan.command[0], "tool", "list"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if completed.returncode != 0:
                return None
            match = re.search(r"(?m)^agent-eyes v([^\s]+)$", completed.stdout)
            return match.group(1) if match else None
        if plan.manager == "pipx":
            completed = subprocess.run(
                [plan.command[0], "list", "--json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if completed.returncode != 0:
                return None
            payload = json.loads(completed.stdout)
            main_package = (
                payload.get("venvs", {})
                .get("agent-eyes", {})
                .get("metadata", {})
                .get("main_package", {})
            )
            version = main_package.get("package_version")
            return str(version) if version else None
    except (
        AttributeError,
        TypeError,
        json.JSONDecodeError,
        OSError,
        subprocess.SubprocessError,
    ):
        return None
    return None


def _launcher_is_usable(plan: InstallPlan) -> bool:
    try:
        launcher = resolve_persistent_executable(plan.manager, plan.command[0])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False
    return launcher.is_file() and (
        sys.platform == "win32" or os.access(launcher, os.X_OK)
    )


def apply_install_plan(
    plan: InstallPlan,
    *,
    consent: Callable[[InstallPlan], bool],
    runner: Callable[[Sequence[str]], object] = _default_runner,
    dry_run: bool = False,
    installed_version: Callable[[InstallPlan], str | None] | None = None,
    launcher_is_usable: Callable[[InstallPlan], bool] | None = None,
    lock_path: Path | None = None,
) -> InstallResult:
    """Apply a displayed plan only after explicit consent."""

    if dry_run:
        return InstallResult(applied=False, dry_run=True)
    if plan.privileged:
        return InstallResult(
            applied=False,
            error="Privileged installation requires a separate explicit OS approval step.",
        )
    if not consent(plan):
        return InstallResult(applied=False, cancelled=True)
    probe = installed_version or _installed_version
    verify_launcher = launcher_is_usable or _launcher_is_usable
    try:
        with setup_process_lock(lock_path):
            if "--force" not in plan.command and probe(plan) == plan.version:
                if not verify_launcher(plan):
                    return InstallResult(
                        applied=False,
                        error=(
                            "Installation metadata is current but the Agent Eyes launcher "
                            "is unavailable. Run an explicit force repair."
                        ),
                    )
                return InstallResult(applied=True, already_current=True)
            try:
                runner(plan.command)
            except subprocess.CalledProcessError as exc:
                return InstallResult(
                    applied=False,
                    error=(
                        f"Installation failed (exit {exc.returncode}). "
                        "Rerun agent-eyes setup for remediation."
                    ),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return InstallResult(
                    applied=False,
                    error=(
                        f"Installation failed: {type(exc).__name__}. "
                        "Rerun agent-eyes setup."
                    ),
                )
            if probe(plan) != plan.version:
                return InstallResult(
                    applied=False,
                    error=(
                        "Installation verification failed: the package manager did not "
                        f"report Agent Eyes {plan.version}. Rerun agent-eyes setup."
                    ),
                )
            if not verify_launcher(plan):
                return InstallResult(
                    applied=False,
                    error=(
                        "Installation verification failed: the Agent Eyes launcher is "
                        "missing or not executable. Run an explicit force repair."
                    ),
                )
            return InstallResult(applied=True)
    except (OSError, RuntimeError) as exc:
        return InstallResult(
            applied=False,
            error=(
                f"Installation setup lock failed: {type(exc).__name__}. "
                "Rerun agent-eyes setup."
            ),
        )


def resolve_persistent_executable(
    manager: str,
    manager_path: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Resolve the stable launcher path created by uv or pipx."""

    if manager == "uv":
        completed = runner(
            [manager_path, "tool", "dir", "--bin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        bin_dir = Path(completed.stdout.strip()).expanduser()
    elif manager == "pipx":
        configured = Path.home() / ".local" / "bin"
        bin_dir = Path(os.environ.get("PIPX_BIN_DIR", configured))
    else:
        raise ValueError(f"Unsupported install manager: {manager}")
    if not str(bin_dir) or not bin_dir.is_absolute():
        raise RuntimeError("Install manager did not report a non-empty absolute bin directory")
    return bin_dir / ("agent-eyes.exe" if sys.platform == "win32" else "agent-eyes")
