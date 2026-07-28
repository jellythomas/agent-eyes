from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agent_eyes import __version__


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_installed_artifact.py"
PLUGIN = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
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


def test_artifact_smoke_uses_platform_filtered_tool_counts() -> None:
    assert smoke_installed_artifact._expected_tool_count("darwin") == 30
    assert smoke_installed_artifact._expected_tool_count("win32") == 27
    assert smoke_installed_artifact._expected_tool_count("linux") == 27


def test_artifact_smoke_declares_transaction_output_and_catalog_budgets() -> None:
    assert smoke_installed_artifact._OUTPUT_LIMITS == {
        "execute": 2 * 1024,
        "observe_target": 4 * 1024,
    }
    assert smoke_installed_artifact._CATALOG_LIMIT_BYTES == 16 * 1024


def test_runtime_and_plugin_versions_match_the_release() -> None:
    plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))

    assert __version__ == "0.10.0"
    assert plugin["version"] == __version__
