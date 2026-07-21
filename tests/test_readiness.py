from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_eyes.setup.readiness import (
    CapabilityProbe,
    ReadinessStateError,
    ReadinessStatus,
    ReadinessStore,
    build_environment_fingerprint,
    derive_readiness,
    probe_readiness,
)


class FakeNativeProvider:
    def __init__(self, *, available: bool = True, permitted: bool = True):
        self.available = available
        self.permitted = permitted

    def is_available(self) -> bool:
        return self.available

    def check_permissions(self) -> tuple[bool, str]:
        if self.permitted:
            return True, "permission granted"
        return False, "permission missing"


class FakeInputProvider:
    def __init__(self, *, available: bool = True):
        self.available = available

    def is_available(self) -> bool:
        return self.available


def available(name: str, *, required: bool = True) -> CapabilityProbe:
    return CapabilityProbe(name=name, required=required, status="available", detail="ok")


def test_required_capabilities_available_is_ready():
    assert derive_readiness([available("native"), available("input")]) is ReadinessStatus.READY


def test_required_missing_capability_requires_setup():
    probes = [
        CapabilityProbe(
            name="native",
            required=True,
            status="missing",
            detail="dependency missing",
            remediation="agent-eyes setup",
        ),
        available("input"),
    ]

    assert derive_readiness(probes) is ReadinessStatus.SETUP_REQUIRED


def test_permission_failure_has_distinct_status():
    probes = [
        CapabilityProbe(
            name="native",
            required=True,
            status="permission_required",
            detail="grant accessibility permission",
            remediation="agent-eyes doctor",
        ),
        available("input"),
    ]

    assert derive_readiness(probes) is ReadinessStatus.PERMISSION_REQUIRED


def test_optional_missing_capability_is_degraded():
    probes = [
        available("native"),
        available("input"),
        CapabilityProbe(
            name="persistent_install",
            required=False,
            status="missing",
            detail="running from a transient environment",
            remediation="agent-eyes setup",
        ),
    ]

    assert derive_readiness(probes) is ReadinessStatus.DEGRADED


def test_live_probe_reports_permission_without_crashing():
    report = probe_readiness(
        native_provider=FakeNativeProvider(permitted=False),
        input_provider=FakeInputProvider(),
        optional_providers=(),
        persistent_executable=None,
    )

    assert report.status is ReadinessStatus.PERMISSION_REQUIRED
    assert report.capability("native_access").status == "permission_required"
    json.dumps(report.to_dict())


def test_live_probe_missing_native_dependency_requires_setup():
    report = probe_readiness(
        native_provider=None,
        input_provider=FakeInputProvider(),
        optional_providers=(),
        persistent_executable=None,
    )

    assert report.status is ReadinessStatus.SETUP_REQUIRED
    assert report.capability("native_access").status == "missing"
    assert report.recovery_command == "agent-eyes setup"


def test_live_probe_optional_provider_does_not_block_core():
    optional = CapabilityProbe(
        name="browser_bridge",
        required=False,
        status="missing",
        detail="bridge not connected",
    )
    report = probe_readiness(
        native_provider=FakeNativeProvider(),
        input_provider=FakeInputProvider(),
        optional_providers=(optional,),
        persistent_executable=Path("/opt/agent-eyes/bin/agent-eyes"),
    )

    assert report.status is ReadinessStatus.DEGRADED
    assert report.core_ready is True


def test_full_profile_requires_a_persistent_runtime(monkeypatch):
    monkeypatch.setattr("agent_eyes.setup.readiness.sys.prefix", "/tmp/uv/archive-123")

    standard = probe_readiness(
        native_provider=FakeNativeProvider(),
        input_provider=FakeInputProvider(),
        profile="standard",
    )
    full = probe_readiness(
        native_provider=FakeNativeProvider(),
        input_provider=FakeInputProvider(),
        profile="full",
    )

    assert standard.status is ReadinessStatus.DEGRADED
    assert full.status is ReadinessStatus.SETUP_REQUIRED
    assert full.capability("persistent_install").required is True


def test_environment_fingerprint_changes_with_runtime_identity():
    first = build_environment_fingerprint(
        version="0.8.0",
        executable="/one/agent-eyes",
        platform_name="darwin",
        architecture="arm64",
        python_version="3.12",
        profile="standard",
    )
    second = build_environment_fingerprint(
        version="0.8.1",
        executable="/two/agent-eyes",
        platform_name="darwin",
        architecture="arm64",
        python_version="3.12",
        profile="standard",
    )

    assert first != second


def test_readiness_store_round_trip_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "readiness.json"
    store = ReadinessStore(path)
    launcher = tmp_path / "agent-eyes"
    launcher.write_text("#!/bin/sh\n")
    report = probe_readiness(
        native_provider=FakeNativeProvider(),
        input_provider=FakeInputProvider(),
        optional_providers=(),
        persistent_executable=launcher,
    )
    replacements: list[tuple[Path, Path]] = []

    from agent_eyes.setup import readiness

    real_replace = readiness.os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(readiness.os, "replace", recording_replace)
    store.save(report)

    assert replacements
    assert replacements[-1][1] == path
    assert store.load()["status"] == "ready"
    assert store.is_current(store.load(), report.fingerprint)


def test_readiness_store_missing_and_corrupt_states(tmp_path):
    path = tmp_path / "readiness.json"
    store = ReadinessStore(path)

    assert store.load() is None
    path.write_text("{not json")

    with pytest.raises(ReadinessStateError):
        store.load()


def test_readiness_store_honors_explicit_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EYES_STATE_DIR", str(tmp_path))

    assert ReadinessStore().path == tmp_path / "readiness.json"


def test_concurrent_manifest_updates_preserve_readiness_and_setup_fields(tmp_path):
    path = tmp_path / "readiness.json"
    store = ReadinessStore(path)
    launcher = tmp_path / "agent-eyes"
    launcher.write_text("#!/bin/sh\n")
    report = probe_readiness(
        native_provider=FakeNativeProvider(),
        input_provider=FakeInputProvider(),
        persistent_executable=launcher,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.save, report),
            pool.submit(
                store.update,
                {
                    "initialized": True,
                    "configured_clients": ["cursor"],
                },
            ),
        ]
        for future in futures:
            future.result()

    payload = store.load()
    assert payload["status"] == "ready"
    assert payload["initialized"] is True
    assert payload["configured_clients"] == ["cursor"]


def test_macos_permission_guidance_names_responsible_launcher():
    from agent_eyes.adapters.macos import MacOSAdapter

    class AX:
        @staticmethod
        def AXIsProcessTrusted():
            return False

    adapter = MacOSAdapter()
    adapter._ax = AX()
    permitted, detail = adapter.check_permissions()

    assert permitted is False
    assert "app that launches Agent Eyes" in detail
    assert "terminal app" not in detail


def test_readiness_lock_rejects_symlink_without_touching_target(tmp_path):
    from agent_eyes.setup import readiness

    victim = tmp_path / "victim"
    victim.write_bytes(b"do not touch")
    victim.chmod(0o644)
    lock_path = tmp_path / ".readiness.lock"
    lock_path.symlink_to(victim)

    with pytest.raises(ReadinessStateError, match="lock file"):
        with readiness._exclusive_state_lock(lock_path):
            pass

    assert victim.read_bytes() == b"do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_windows_readiness_lock_has_bounded_nonblocking_acquisition():
    from agent_eyes.setup import readiness

    class Stream:
        def __init__(self):
            self.content = b"\0"

        def seek(self, _offset):
            return None

        def read(self, _count):
            return self.content

        def write(self, value):
            self.content += value

        def flush(self):
            return None

        def fileno(self):
            return 17

    class Msvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.modes: list[int] = []

        def locking(self, _fd, mode, _nbytes):
            self.modes.append(mode)
            raise OSError("busy")

    msvcrt = Msvcrt()

    with pytest.raises(ReadinessStateError, match="Timed out"):
        readiness._acquire_windows_file_lock(
            Stream(),
            msvcrt,
            timeout=0,
        )

    assert msvcrt.modes == [msvcrt.LK_NBLCK]
