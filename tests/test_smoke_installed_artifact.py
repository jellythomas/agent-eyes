from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_installed_artifact.py"
SPEC = importlib.util.spec_from_file_location("smoke_installed_artifact", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke_installed_artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke_installed_artifact)


def test_artifact_smoke_environment_rejects_python_import_injection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/tmp/fake-python-home")
    monkeypatch.setenv("PYTHONPATH", "/tmp/fake-python-path")
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)

    environment = smoke_installed_artifact._isolated_environment(tmp_path)

    assert "PYTHONHOME" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["AGENT_EYES_STATE_DIR"] == str(tmp_path / "state")
