from __future__ import annotations

import os

import pytest

from agent_eyes.setup import state
from agent_eyes.setup.readiness import ReadinessStore, probe_readiness


class Native:
    def is_available(self):
        return True

    def check_permissions(self):
        return True, "granted"


class Input:
    def is_available(self):
        return True


def test_configuration_and_readiness_share_one_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_dir", lambda: tmp_path)
    state.mark_initialized("0.8.0", ["cursor"], [])

    manifest = tmp_path / "readiness.json"
    assert manifest.exists()
    assert not (tmp_path / "state.json").exists()

    launcher = tmp_path / "agent-eyes"
    launcher.write_text("#!/bin/sh\n")
    report = probe_readiness(
        native_provider=Native(),
        input_provider=Input(),
        persistent_executable=launcher,
    )
    ReadinessStore(manifest).save(report)

    loaded = state.get_state()
    assert loaded["initialized"] is True
    assert loaded["tools_configured"] == ["cursor"]
    assert loaded["status"] == "ready"


def test_any_agent_eyes_version_change_invalidates_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_dir", lambda: tmp_path)
    state.mark_initialized("0.8.0", ["cursor"], [])

    assert state.needs_rescan("0.8.0") is False
    assert state.needs_rescan("0.8.1") is True


def test_reading_missing_state_is_side_effect_free(tmp_path, monkeypatch):
    state_dir = tmp_path / "missing-state"
    monkeypatch.setattr(state, "_state_dir", lambda: state_dir)

    loaded = state.get_state()

    assert loaded["initialized"] is False
    assert not state_dir.exists()


def test_state_write_creates_private_directory_and_manifest(tmp_path, monkeypatch):
    state_dir = tmp_path / "private-state"
    monkeypatch.setattr(state, "_state_dir", lambda: state_dir)

    state.save_state({"initialized": False})

    assert state_dir.is_dir()
    assert (state_dir / "readiness.json").is_file()
    if os.name != "nt":
        assert state_dir.stat().st_mode & 0o777 == 0o700
        assert (state_dir / "readiness.json").stat().st_mode & 0o777 == 0o600


def test_initialized_state_merges_clients_from_idempotent_setup_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(state, "_state_dir", lambda: tmp_path)

    state.mark_initialized("0.8.0", ["cursor"], [])
    state.mark_initialized("0.8.0", ["codex"], [])

    loaded = state.get_state()
    assert loaded["tools_configured"] == ["codex", "cursor"]


def test_setup_lock_rejects_symlink_without_mutating_target(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("keep")
    original_mode = target.stat().st_mode
    lock_path = tmp_path / ".setup.lock"
    lock_path.symlink_to(target)

    try:
        with state.setup_process_lock(lock_path):
            raise AssertionError("symlinked lock unexpectedly acquired")
    except RuntimeError as exc:
        assert "must not be a symlink" in str(exc)

    assert target.read_text() == "keep"
    assert target.stat().st_mode == original_mode


def test_setup_lock_rejects_a_hard_link_without_mutating_target(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("keep")
    original_mode = target.stat().st_mode
    lock_path = tmp_path / ".setup.lock"
    try:
        os.link(target, lock_path)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(RuntimeError, match="must not have multiple links"):
        with state.setup_process_lock(lock_path):
            pytest.fail("hard-linked lock unexpectedly acquired")

    assert target.read_text() == "keep"
    assert target.stat().st_mode == original_mode


def test_windows_setup_lock_has_bounded_nonblocking_acquisition(tmp_path):
    class Stream:
        def seek(self, _offset):
            return None

        def read(self, _count):
            return b"\0"

        def fileno(self):
            return 17

    class Msvcrt:
        LK_NBLCK = 1

        def __init__(self):
            self.modes: list[int] = []

        def locking(self, _fd, mode, _nbytes):
            self.modes.append(mode)
            raise OSError("persistent lock failure")

    msvcrt = Msvcrt()
    lock_path = tmp_path / ".setup.lock"

    with pytest.raises(RuntimeError, match="Timed out waiting for setup lock"):
        state._acquire_windows_setup_lock(
            Stream(),
            msvcrt,
            label="Setup lock",
            path=lock_path,
            timeout=0,
        )

    assert msvcrt.modes == [msvcrt.LK_NBLCK]


def test_state_save_never_opens_a_symlinked_readiness_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_dir", lambda: tmp_path)
    victim = tmp_path / "unrelated"
    victim.write_text("keep")
    original_mode = victim.stat().st_mode
    readiness_lock = tmp_path / ".readiness.json.lock"
    readiness_lock.symlink_to(victim)

    with pytest.raises(RuntimeError, match="Readiness lock must not be a symlink"):
        state.save_state({"initialized": False})

    assert victim.read_text() == "keep"
    assert victim.stat().st_mode == original_mode
    assert readiness_lock.is_symlink()
    assert not (tmp_path / "readiness.json").exists()


def test_readiness_lock_retargeted_after_validation_fails_without_touching_target(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(state, "_state_dir", lambda: tmp_path)
    victim = tmp_path / "unrelated"
    victim.write_text("keep")
    original_mode = victim.stat().st_mode
    readiness_lock = tmp_path / ".readiness.json.lock"
    real_validate = state._validate_readiness_lock_target

    def validate_then_retarget():
        real_validate()
        readiness_lock.symlink_to(victim)

    monkeypatch.setattr(
        state,
        "_validate_readiness_lock_target",
        validate_then_retarget,
    )

    with pytest.raises(
        RuntimeError,
        match=r"Readiness lock (?:could not be opened safely|must not be a symlink)",
    ):
        state.save_state({"initialized": False})

    assert victim.read_text() == "keep"
    assert victim.stat().st_mode == original_mode
    assert not (tmp_path / "readiness.json").exists()
