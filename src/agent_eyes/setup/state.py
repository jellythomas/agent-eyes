"""Compatibility helpers over the unified readiness manifest."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .readiness import (
    ReadinessStateError,
    ReadinessStore,
    SCHEMA_VERSION,
    _READINESS_LOCK_TIMEOUT_SECONDS,
    _READINESS_THREAD_LOCK,
    _acquire_windows_file_lock,
)


def _state_dir() -> Path:
    """Return the state directory without creating it."""
    return Path(
        os.environ.get("AGENT_EYES_STATE_DIR", str(Path.home() / ".agent-eyes"))
    )


def _state_file() -> Path:
    return _state_dir() / "readiness.json"


def _legacy_state_file() -> Path:
    return _state_dir() / "state.json"


def _readiness_lock_file() -> Path:
    state_file = _state_file()
    return state_file.with_name(f".{state_file.name}.lock")


def _validate_readiness_lock_target() -> None:
    lock_path = _readiness_lock_file()
    if lock_path.is_symlink():
        raise RuntimeError(f"Readiness lock must not be a symlink: {lock_path}")
    if lock_path.exists() and not lock_path.is_file():
        raise RuntimeError(f"Readiness lock must be a regular file: {lock_path}")


def _acquire_windows_setup_lock(
    stream,
    msvcrt_module,
    *,
    label: str,
    path: Path,
    timeout: float = _READINESS_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Acquire a Windows setup lock without retrying permanent errors forever."""
    try:
        _acquire_windows_file_lock(stream, msvcrt_module, timeout=timeout)
    except ReadinessStateError as exc:
        raise RuntimeError(
            f"Timed out waiting for {label.lower()}: {path}"
        ) from exc


@contextmanager
def _exclusive_file_lock(path: Path, *, label: str) -> Iterator[None]:
    """Open without following the final symlink and wait for an OS byte lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _READINESS_THREAD_LOCK:
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(f"{label} could not be opened safely: {path}") from exc
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise RuntimeError(f"{label} must be a regular file: {path}")
            if identity.st_nlink != 1:
                raise RuntimeError(f"{label} must not have multiple links: {path}")
            try:
                path_identity = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"{label} could not be verified: {path}") from exc
            if (identity.st_dev, identity.st_ino) != (
                path_identity.st_dev,
                path_identity.st_ino,
            ):
                raise RuntimeError(f"{label} must not be a symlink: {path}")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)

            with os.fdopen(descriptor, "a+b") as stream:
                descriptor = -1
                if os.name == "nt":
                    import msvcrt

                    _acquire_windows_setup_lock(
                        stream,
                        msvcrt,
                        label=label,
                        path=path,
                    )
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "initialized": False,
        "initialized_at": None,
        "version": None,
        "tools_configured": [],
        "competitors_replaced": [],
    }


def _ensure_private_state_dir() -> Path:
    directory = _state_dir()
    if directory.is_symlink():
        raise RuntimeError(f"Agent Eyes state directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not directory.is_dir():
        raise RuntimeError(f"Agent Eyes state path is not a directory: {directory}")
    try:
        directory.chmod(0o700)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to make Agent Eyes state directory private: {directory}"
        ) from exc
    return directory


def get_state() -> dict:
    """Read the current setup state."""
    store = ReadinessStore(_state_file())
    try:
        current = store.load()
    except ReadinessStateError:
        current = None
    if current is not None:
        return {**_default_state(), **current}

    legacy = _legacy_state_file()
    if legacy.exists():
        import json

        try:
            payload = json.loads(legacy.read_text())
        except (json.JSONDecodeError, OSError):
            payload = None
        if isinstance(payload, dict):
            return {**_default_state(), **payload}
    return _default_state()


def save_state(state: dict) -> None:
    """Persist setup state."""
    _ensure_private_state_dir()
    with setup_process_lock():
        _validate_readiness_lock_target()
        with _exclusive_file_lock(
            _readiness_lock_file(),
            label="Readiness lock",
        ):
            ReadinessStore(_state_file())._update_locked(
                {**_default_state(), **state}
            )


def is_first_run() -> bool:
    """Check if this is the first time agent-eyes is running."""
    return not get_state().get("initialized", False)


def needs_rescan(current_version: str) -> bool:
    """Revalidate setup whenever the Agent Eyes version changes."""
    state = get_state()
    if not state.get("initialized"):
        return True

    return state.get("version") != current_version


def mark_initialized(
    version: str,
    tools_configured: list[str],
    competitors_replaced: list[str],
) -> None:
    """Mark setup as complete."""
    _ensure_private_state_dir()
    with setup_process_lock():
        _validate_readiness_lock_target()
        with _exclusive_file_lock(
            _readiness_lock_file(),
            label="Readiness lock",
        ):
            current = get_state()
            same_version = current.get("version") == version
            prior_tools = current.get("tools_configured", []) if same_version else []
            prior_competitors = (
                current.get("competitors_replaced", []) if same_version else []
            )
            merged_tools = sorted(
                {*prior_tools, *tools_configured}
            )
            merged_competitors = sorted(
                {*prior_competitors, *competitors_replaced}
            )
            ReadinessStore(_state_file())._update_locked({
                "initialized": True,
                "initialized_at": datetime.now(timezone.utc).isoformat(),
                "version": version,
                "tools_configured": merged_tools,
                "competitors_replaced": merged_competitors,
            })


def get_setup_lock_path() -> Path:
    """Return the process-lock path without creating state on previews."""
    return _state_dir() / ".setup.lock"


@contextmanager
def setup_process_lock(lock_path: Path | None = None) -> Iterator[None]:
    """Serialize setup mutations across threads and processes."""
    selected_path = lock_path
    if selected_path is None:
        _ensure_private_state_dir()
        selected_path = get_setup_lock_path()
    if selected_path.is_symlink():
        raise RuntimeError(f"Setup lock must not be a symlink: {selected_path}")
    if selected_path.exists() and not selected_path.is_file():
        raise RuntimeError(f"Setup lock must be a regular file: {selected_path}")
    with _exclusive_file_lock(selected_path, label="Setup lock"):
        yield


def get_backups_path() -> Path:
    """Return the backup path without creating it."""
    return _state_dir() / "backups"


def get_backups_dir() -> Path:
    """Get the backups directory for config file backups."""
    _ensure_private_state_dir()
    d = get_backups_path()
    if d.is_symlink():
        raise RuntimeError(f"Agent Eyes backups directory must not be a symlink: {d}")
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d
