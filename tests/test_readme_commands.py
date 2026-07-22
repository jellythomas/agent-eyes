from __future__ import annotations

from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


def test_readme_uses_canonical_guided_setup_commands():
    content = README.read_text(encoding="utf-8")

    assert "uvx agent-eyes setup" in content
    assert "agent-eyes doctor" in content
    assert "agent-eyes install" in content
    assert "agent-eyes init" in content
    assert '"args": ["serve"]' in content


def test_readme_removes_obsolete_model_and_chrome_specific_setup():
    content = README.read_text(encoding="utf-8")

    assert "/agent-eyes-init" not in content
    assert "install.json" not in content
    assert "Chrome Extension (recommended) or Chrome with" not in content


def test_readme_documents_foreground_browser_reuse_and_explicit_shadow():
    content = README.read_text(encoding="utf-8").lower()

    assert "reuse" in content
    assert "open tabs" in content
    assert "browser-agnostic" in content
    assert "explicit" in content and "shadow" in content


def test_readme_documents_readiness_and_recovery():
    content = README.read_text(encoding="utf-8")

    assert "setup_required" in content
    assert "permission_required" in content
    assert "degraded" in content
    assert "~/.agent-eyes/readiness.json" in content


def test_readme_documents_event_completion_stable_targets_and_safe_config():
    content = README.read_text(encoding="utf-8").lower()

    assert "axobserver" in content
    assert "ui automation" in content
    assert "at-spi" in content
    assert "provider-qualified ids" in content
    assert "jsonc" in content
    assert "symlink" in content
    assert "healthy current launcher without reinstalling" in content


def test_readme_documents_attach_before_setup_without_in_mcp_installation():
    content = README.read_text(encoding="utf-8").lower()

    assert "attach the mcp before running setup" in content
    assert "stdio handshake still completes" in content
    assert "never installs packages from inside a" in content


def test_readme_reports_platform_aware_catalog_and_reproducible_benchmarks():
    content = README.read_text(encoding="utf-8")

    assert "28 tools on macOS and 26 on Windows/Linux" in content
    assert "benchmarks/benchmark_startup.py" in content
    assert "benchmarks/benchmark_runtime.py" in content
    assert "benchmarks/benchmark_cdp_bounds.py" in content
    assert "benchmarks/benchmark_journeys.py" in content
    assert "benchmarks/stress_concurrency.py" in content
