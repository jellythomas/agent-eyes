"""Live, side-effect-free Agent Eyes readiness checks."""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent_eyes import __version__


SCHEMA_VERSION = 1
RECOVERY_COMMAND = "agent-eyes setup"
_VALID_PROBE_STATUSES = {
    "available",
    "missing",
    "permission_required",
    "error",
}


class ReadinessStatus(str, Enum):
    """Aggregate runtime readiness exposed by both CLI and MCP."""

    READY = "ready"
    DEGRADED = "degraded"
    SETUP_REQUIRED = "setup_required"
    PERMISSION_REQUIRED = "permission_required"


class ReadinessStateError(RuntimeError):
    """Raised when a persisted readiness manifest cannot be trusted."""


@dataclass(frozen=True)
class CapabilityProbe:
    """Serializable result of checking one runtime capability."""

    name: str
    required: bool
    status: str
    detail: str
    remediation: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_PROBE_STATUSES:
            raise ValueError(f"Unsupported capability status: {self.status}")

    @property
    def available(self) -> bool:
        return self.status == "available"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReadinessReport:
    """A probe-derived, JSON-safe runtime readiness snapshot."""

    status: ReadinessStatus
    capabilities: tuple[CapabilityProbe, ...]
    fingerprint: dict[str, str]
    checked_at: str
    agent_eyes_version: str = __version__
    schema_version: int = SCHEMA_VERSION
    recovery_command: str = RECOVERY_COMMAND

    @property
    def core_ready(self) -> bool:
        return all(
            probe.available
            for probe in self.capabilities
            if probe.required
        )

    def capability(self, name: str) -> CapabilityProbe:
        for probe in self.capabilities:
            if probe.name == name:
                return probe
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_eyes_version": self.agent_eyes_version,
            "checked_at": self.checked_at,
            "fingerprint": dict(self.fingerprint),
            "status": self.status.value,
            "core_ready": self.core_ready,
            "recovery_command": self.recovery_command,
            "capabilities": [probe.to_dict() for probe in self.capabilities],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReadinessReport":
        """Rehydrate a launcher-produced JSON report without probing locally."""
        capabilities = tuple(
            CapabilityProbe(
                name=str(item["name"]),
                required=bool(item.get("required", False)),
                status=str(item["status"]),
                detail=str(item.get("detail", "")),
                remediation=item.get("remediation"),
                version=item.get("version"),
            )
            for item in payload.get("capabilities", [])
            if isinstance(item, Mapping)
        )
        return cls(
            status=ReadinessStatus(str(payload["status"])),
            capabilities=capabilities,
            fingerprint=dict(payload.get("fingerprint", {})),
            checked_at=str(payload.get("checked_at", "")),
            agent_eyes_version=str(payload.get("agent_eyes_version", __version__)),
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            recovery_command=str(payload.get("recovery_command", RECOVERY_COMMAND)),
        )

    def to_text(self, *, verbose: bool = False) -> str:
        unavailable = [probe for probe in self.capabilities if not probe.available]
        if not verbose:
            summary = f"{self.status.value} | core:{'ready' if self.core_ready else 'blocked'}"
            if unavailable:
                summary += " | " + ", ".join(
                    f"{probe.name}:{probe.status}" for probe in unavailable
                )
            if self.status is not ReadinessStatus.READY:
                summary += f" | run: {self.recovery_command}"
            return summary

        lines = [f"Agent Eyes {self.agent_eyes_version}: {self.status.value}"]
        for probe in self.capabilities:
            marker = "ok" if probe.available else probe.status
            lines.append(f"- {probe.name}: {marker} — {probe.detail}")
            if probe.remediation and not probe.available:
                lines.append(f"  Fix: {probe.remediation}")
        return "\n".join(lines)


def derive_readiness(probes: Sequence[CapabilityProbe]) -> ReadinessStatus:
    """Derive one truthful status from required and optional capabilities."""

    required = [probe for probe in probes if probe.required]
    if any(probe.status in {"missing", "error"} for probe in required):
        return ReadinessStatus.SETUP_REQUIRED
    if any(probe.status == "permission_required" for probe in required):
        return ReadinessStatus.PERMISSION_REQUIRED
    if any(not probe.available for probe in probes):
        return ReadinessStatus.DEGRADED
    return ReadinessStatus.READY


def build_environment_fingerprint(
    *,
    version: str = __version__,
    executable: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    architecture: str | None = None,
    python_version: str | None = None,
    profile: str = "standard",
) -> dict[str, str]:
    """Build the identity that invalidates cached readiness state."""

    return {
        "agent_eyes_version": version,
        "platform": platform_name or sys.platform,
        "architecture": architecture or platform.machine() or "unknown",
        "python": python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
        "executable": str(executable or sys.executable),
        "profile": profile,
    }


def _native_remediation(platform_name: str) -> str:
    if platform_name == "darwin":
        return "Install the macOS provider and grant Accessibility permission with agent-eyes setup"
    if platform_name == "win32":
        return "Install the Windows UI Automation provider with agent-eyes setup"
    if platform_name == "linux":
        return "Install AT-SPI2/PyGObject and the Linux provider with agent-eyes setup"
    return RECOVERY_COMMAND


def _is_transient_uv_runtime(prefix: str | os.PathLike[str] | None = None) -> bool:
    normalized = str(prefix or sys.prefix).replace("\\", "/")
    return "/uv/archive-" in normalized or "/uv/archive/" in normalized


def _persistent_runtime_probe(
    persistent_executable: str | os.PathLike[str] | None,
    *,
    required: bool = False,
) -> CapabilityProbe:
    if persistent_executable is not None and Path(persistent_executable).exists():
        return CapabilityProbe(
            name="persistent_install",
            required=required,
            status="available",
            detail=f"persistent launcher: {persistent_executable}",
        )
    if persistent_executable is not None:
        return CapabilityProbe(
            name="persistent_install",
            required=required,
            status="missing",
            detail=f"persistent launcher does not exist: {persistent_executable}",
            remediation=RECOVERY_COMMAND,
        )
    if _is_transient_uv_runtime():
        return CapabilityProbe(
            name="persistent_install",
            required=required,
            status="missing",
            detail="running from a disposable uvx environment",
            remediation=RECOVERY_COMMAND,
        )
    return CapabilityProbe(
        name="persistent_install",
        required=required,
        status="available",
        detail=f"stable runtime: {sys.prefix}",
    )


def probe_native_capability(
    native_provider: Any | None,
    *,
    platform_name: str | None = None,
) -> CapabilityProbe:
    """Probe native accessibility on the provider's owning thread."""

    current_platform = platform_name or sys.platform
    if native_provider is None:
        return CapabilityProbe(
            name="native_access",
            required=True,
            status="missing",
            detail="native accessibility provider is unavailable",
            remediation=_native_remediation(current_platform),
        )

    try:
        provider_available = bool(native_provider.is_available())
    except Exception as exc:
        return CapabilityProbe(
            name="native_access",
            required=True,
            status="error",
            detail=f"native provider check failed: {type(exc).__name__}",
            remediation=RECOVERY_COMMAND,
        )
    if not provider_available:
        return CapabilityProbe(
            name="native_access",
            required=True,
            status="missing",
            detail="native accessibility provider is unavailable",
            remediation=_native_remediation(current_platform),
        )

    try:
        permitted, permission_detail = native_provider.check_permissions()
    except Exception as exc:
        return CapabilityProbe(
            name="native_access",
            required=True,
            status="error",
            detail=f"permission check failed: {type(exc).__name__}",
            remediation=RECOVERY_COMMAND,
        )
    return CapabilityProbe(
        name="native_access",
        required=True,
        status="available" if permitted else "permission_required",
        detail=str(permission_detail),
        remediation=None if permitted else RECOVERY_COMMAND,
    )


def probe_input_capability(input_provider: Any | None) -> CapabilityProbe:
    """Probe physical input on the provider's owning thread."""

    if input_provider is None:
        input_available = False
        input_detail = "input provider is unavailable"
    else:
        try:
            input_available = bool(input_provider.is_available())
            input_detail = (
                f"input provider: {input_provider.__class__.__name__}"
                if input_available
                else "input provider is unavailable"
            )
        except Exception as exc:
            input_available = False
            input_detail = f"input provider check failed: {type(exc).__name__}"
    return CapabilityProbe(
        name="input",
        required=True,
        status="available" if input_available else "missing",
        detail=input_detail,
        remediation=None if input_available else RECOVERY_COMMAND,
    )


def compose_readiness_report(
    *,
    native_probe: CapabilityProbe,
    input_probe: CapabilityProbe,
    optional_providers: Iterable[CapabilityProbe] = (),
    persistent_executable: str | os.PathLike[str] | None = None,
    profile: str = "standard",
    version: str = __version__,
    platform_name: str | None = None,
) -> ReadinessReport:
    """Compose already-probed capabilities without touching provider objects."""

    current_platform = platform_name or sys.platform
    probes = [
        native_probe,
        input_probe,
        _persistent_runtime_probe(
            persistent_executable,
            required=profile == "full",
        ),
        *optional_providers,
    ]
    fingerprint = build_environment_fingerprint(
        version=version,
        executable=persistent_executable or sys.executable,
        platform_name=current_platform,
        profile=profile,
    )
    return ReadinessReport(
        status=derive_readiness(probes),
        capabilities=tuple(probes),
        fingerprint=fingerprint,
        checked_at=datetime.now(timezone.utc).isoformat(),
        agent_eyes_version=version,
    )


def probe_readiness(
    *,
    native_provider: Any | None,
    input_provider: Any | None,
    optional_providers: Iterable[CapabilityProbe] = (),
    persistent_executable: str | os.PathLike[str] | None = None,
    profile: str = "standard",
    version: str = __version__,
    platform_name: str | None = None,
) -> ReadinessReport:
    """Probe injected runtime providers without installing or launching anything."""
    return compose_readiness_report(
        native_probe=probe_native_capability(
            native_provider,
            platform_name=platform_name,
        ),
        input_probe=probe_input_capability(input_provider),
        optional_providers=optional_providers,
        persistent_executable=persistent_executable,
        profile=profile,
        version=version,
        platform_name=platform_name,
    )


def load_native_provider(platform_name: str | None = None) -> Any | None:
    """Construct the current platform adapter without runtime installation."""

    current_platform = platform_name or sys.platform
    try:
        if current_platform == "darwin":
            from agent_eyes.adapters.macos import MacOSAdapter

            provider = MacOSAdapter()
        elif current_platform == "win32":
            from agent_eyes.adapters.windows import WindowsAdapter

            provider = WindowsAdapter()
        elif current_platform == "linux":
            from agent_eyes.adapters.linux import LinuxAdapter

            provider = LinuxAdapter()
        else:
            return None
        return provider if provider.is_available() else None
    except Exception:
        return None


def load_input_provider() -> Any | None:
    """Construct the current input backend without letting probe errors escape."""

    try:
        from agent_eyes.input_sim import get_input_backend

        return get_input_backend()
    except Exception:
        return None


def probe_current_readiness(
    *,
    native_provider: Any | None = None,
    input_provider: Any | None = None,
    persistent_executable: str | os.PathLike[str] | None = None,
    profile: str = "standard",
) -> ReadinessReport:
    """Convenience wrapper used by the CLI and MCP server."""

    native = native_provider if native_provider is not None else load_native_provider()
    input_backend = input_provider if input_provider is not None else load_input_provider()
    return probe_readiness(
        native_provider=native,
        input_provider=input_backend,
        persistent_executable=persistent_executable,
        profile=profile,
    )


_READINESS_THREAD_LOCK = threading.RLock()
_READINESS_LOCK_TIMEOUT_SECONDS = 5.0


def _open_readiness_lock(path: Path):
    """Open one regular lock file without following a symlink."""
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, current):
            raise ReadinessStateError("Readiness lock file must be a regular file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        # msvcrt.locking uses the descriptor's raw position, so keep it aligned.
        return os.fdopen(descriptor, "a+b", buffering=0)
    except ReadinessStateError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ReadinessStateError(
            "Readiness lock file could not be opened safely"
        ) from exc


def _acquire_windows_file_lock(
    stream,
    msvcrt_module,
    *,
    timeout: float = _READINESS_LOCK_TIMEOUT_SECONDS,
    poll_interval: float = 0.05,
) -> None:
    """Acquire the one-byte Windows lock with a caller-visible deadline."""
    stream.seek(0)
    if stream.read(1) == b"":
        stream.write(b"\0")
        stream.flush()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        stream.seek(0)
        try:
            msvcrt_module.locking(
                stream.fileno(),
                msvcrt_module.LK_NBLCK,
                1,
            )
            return
        except OSError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadinessStateError(
                    "Timed out waiting for the readiness state lock"
                ) from exc
            time.sleep(min(max(0.0, poll_interval), remaining))


@contextmanager
def _exclusive_state_lock(path: Path):
    """Serialize readiness read/merge/write across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _READINESS_THREAD_LOCK:
        with _open_readiness_lock(path) as stream:
            if os.name == "nt":
                import msvcrt

                _acquire_windows_file_lock(stream, msvcrt)
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


class ReadinessStore:
    """Atomic storage for the latest verified readiness snapshot."""

    def __init__(self, path: Path | None = None):
        state_dir = Path(
            os.environ.get("AGENT_EYES_STATE_DIR", str(Path.home() / ".agent-eyes"))
        )
        self.path = path or (state_dir / "readiness.json")

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ReadinessStateError(
                f"Cannot read readiness state; rerun {RECOVERY_COMMAND}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ReadinessStateError(
                f"Unsupported readiness state; rerun {RECOVERY_COMMAND}"
            )
        return payload

    def save(self, report: ReadinessReport | Mapping[str, Any]) -> None:
        incoming = report.to_dict() if isinstance(report, ReadinessReport) else dict(report)
        self.update(incoming)

    def update(self, changes: Mapping[str, Any]) -> None:
        """Atomically merge fields into the shared readiness/setup manifest."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _exclusive_state_lock(lock_path):
            self._update_locked(changes)

    def _update_locked(self, changes: Mapping[str, Any]) -> None:
        try:
            existing = self.load() or {}
        except ReadinessStateError:
            existing = {}
        payload = {**existing, **dict(changes)}
        payload["schema_version"] = SCHEMA_VERSION
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".readiness.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                raise

    @staticmethod
    def is_current(payload: Mapping[str, Any] | None, fingerprint: Mapping[str, str]) -> bool:
        if not payload:
            return False
        return (
            payload.get("schema_version") == SCHEMA_VERSION
            and payload.get("fingerprint") == dict(fingerprint)
        )
