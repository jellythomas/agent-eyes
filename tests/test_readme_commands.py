from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
SKILLS = (
    ROOT / "skills" / "agent-eyes" / "SKILL.md",
    ROOT / "skills" / "install" / "SKILL.md",
    ROOT / "skills" / "init" / "SKILL.md",
)


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_has_an_ordered_install_to_ready_path():
    content = _readme()
    steps = (
        "### 1. Install and verify the prerequisites",
        "### 2. Preview setup",
        "### 3. Apply setup",
        "### 4. Verify the installed version and readiness",
        "### 5. Restart changed MCP clients",
        "### 6. Make the first MCP calls",
    )

    positions = [content.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "uv --version" in content
    assert "uvx agent-eyes@latest setup --dry-run" in content
    assert "uvx agent-eyes@latest setup" in content
    assert "agent-eyes --version" in content
    assert "agent-eyes doctor --verbose" in content
    assert "`ready`" in content
    assert "`status`" in content
    assert "`list_tabs`" in content


def test_readme_explains_exactly_what_setup_can_and_cannot_do():
    content = _readme().lower()

    assert "ephemeral" in content and "uvx" in content
    assert "persistent" in content and "platform" in content
    assert "claude code and codex" in content
    assert "agents/openai.yaml" in content
    assert "absolute" in content and 'args": ["serve"]' in content
    assert "does not install `uv`" in content
    assert "does not run `sudo`" in content
    assert "cannot grant" in content and "permission" in content
    assert "cannot approve" in content and "dialog" in content
    assert "does not remove" in content and "mcp" in content


def test_readme_lists_every_supported_client_id_and_config_type():
    content = _readme()
    client_ids = (
        "claude-code",
        "claude-desktop",
        "codex",
        "cursor",
        "vscode",
        "cline",
        "roo-code",
        "windsurf",
        "zed",
        "continue",
    )

    for client_id in client_ids:
        assert f"`{client_id}`" in content
    assert "JSON (`mcpServers`)" in content
    assert "JSON (`servers`)" in content
    assert "JSON (`context_servers`)" in content
    assert "TOML (`mcp_servers`)" in content


def test_readme_links_common_syntax_to_exhaustive_references():
    content = _readme()

    assert (
        "[complete CLI reference](https://github.com/jellythomas/agent-eyes/blob/"
        "main/docs/api/agent-eyes-cli.md)"
    ) in content
    assert (
        "[complete MCP tool reference](https://github.com/jellythomas/agent-eyes/"
        "blob/main/docs/api/mcp-tools.md)"
    ) in content
    assert "agent-eyes setup" in content
    assert "agent-eyes doctor" in content
    assert "agent-eyes install" in content
    assert "agent-eyes init" in content
    assert "agent-eyes serve" in content
    assert '"args": ["serve"]' in content


def test_readme_documents_native_first_browser_reuse_and_explicit_shadow():
    content = _readme().lower()

    assert "inspect all open tabs" in content
    assert "reuse" in content
    assert "browser-agnostic" in content
    assert "open a new foreground tab" in content
    assert "shadow=true" in content
    assert "explicit" in content and "background" in content
    assert "does not depend on playwright mcp" in content
    assert "chrome remote-debugging restart" in content


def test_readme_preserves_readiness_platform_and_performance_guidance():
    content = _readme()
    lowercase = content.lower()

    assert "setup_required" in content
    assert "permission_required" in content
    assert "degraded" in content
    assert "~/.agent-eyes/readiness.json" in content
    assert "AXObserver" in content
    assert "UI Automation" in content
    assert "AT-SPI" in content
    assert "provider-qualified" in lowercase
    assert "fixed sleep" in lowercase
    assert "benchmarks/benchmark_startup.py" in content
    assert "benchmarks/benchmark_runtime.py" in content
    assert "benchmarks/benchmark_cdp_bounds.py" in content
    assert "benchmarks/benchmark_journeys.py" in content
    assert "benchmarks/stress_concurrency.py" in content


def test_bundled_skills_use_the_latest_bootstrap_and_correct_boundaries():
    for skill_path in SKILLS:
        content = skill_path.read_text(encoding="utf-8")
        assert "uvx agent-eyes@latest setup" in content, skill_path

    install = SKILLS[1].read_text(encoding="utf-8").lower()
    init = SKILLS[2].read_text(encoding="utf-8").lower()
    assert "does not configure mcp clients or install skills" in install
    assert "does not install or repair the runtime" in init
