from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_eyes.setup import configurator
from agent_eyes.setup.configurator import (
    InvalidConfigError,
    apply_mcp_plan,
    configure_mcp_file,
    preflight_mcp_file,
    preflight_text_file,
)
from agent_eyes.setup.templates.mcp_entry import (
    AGENT_EYES_TOOLS,
    _agent_eyes_tools_for_platform,
    get_mcp_entry,
    get_mcp_entry_zed,
)
from agent_eyes.setup.templates.claude_md import CLAUDE_MD_SECTION
from agent_eyes.setup.templates.skill import SKILL_MD
from agent_eyes.setup.templates.openai_skill import OPENAI_YAML


def test_generated_skill_teaches_native_reuse_and_explicit_shadow():
    normalized = " ".join(SKILL_MD.split())
    assert "all browsers" in normalized
    assert "stable provider-qualified target IDs" in normalized
    assert "fixed sleeps" in normalized
    assert "shadow=true" in normalized
    assert "restart Chrome" in normalized
    assert "eyes_list_chrome_tabs" not in normalized


def test_checked_in_agent_skill_matches_the_installer_template():
    source = Path(__file__).resolve().parents[1] / "skills" / "agent-eyes" / "SKILL.md"

    assert source.read_text(encoding="utf-8") == SKILL_MD


def test_checked_in_codex_skill_metadata_matches_the_installer_template():
    source = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "agent-eyes"
        / "agents"
        / "openai.yaml"
    )

    assert source.read_text(encoding="utf-8") == OPENAI_YAML


def test_generated_claude_guidance_uses_current_tool_names_and_policy():
    assert "mcp__agent-eyes__list_tabs" in CLAUDE_MD_SECTION
    assert "reuse relevant open tabs across browsers" in CLAUDE_MD_SECTION
    assert "event-backed `wait`" in CLAUDE_MD_SECTION
    assert "eyes_list_chrome_tabs" not in CLAUDE_MD_SECTION


EXECUTABLE = (
    r"C:\Users\example\.local\bin\agent-eyes.exe"
    if os.name == "nt"
    else "/Users/example/.local/bin/agent-eyes"
)


@pytest.fixture(autouse=True)
def _isolate_setup_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EYES_STATE_DIR", str(tmp_path / "state"))


def _apply_plan_in_process(plan, backups_dir: str, lock_path: str, result_queue):
    """Process entry point used to prove the setup lock is cross-process."""
    try:
        result = configurator.apply_mcp_plan(
            plan,
            backups_dir=Path(backups_dir),
            lock_path=Path(lock_path),
        )
        result_queue.put((result.applied, result.changed, result.backup, None))
    except Exception as exc:  # pragma: no cover - reported in the parent assertion
        result_queue.put((False, False, None, f"{type(exc).__name__}: {exc}"))


def test_plain_skill_file_uses_the_same_preflighted_atomic_transaction(tmp_path):
    skill_file = tmp_path / "skills" / "agent-eyes" / "SKILL.md"

    plan = preflight_text_file(
        skill_file,
        content=SKILL_MD,
        source_format="skill",
    )

    assert plan.changed is True
    assert not skill_file.exists()
    result = apply_mcp_plan(
        plan,
        backups_dir=tmp_path / "backups",
        lock_path=tmp_path / ".setup.lock",
    )
    assert result.applied is True
    assert skill_file.read_text(encoding="utf-8") == SKILL_MD

    current = preflight_text_file(
        skill_file,
        content=SKILL_MD,
        source_format="skill",
    )
    assert current.changed is False


def test_generated_entry_uses_persistent_serve_command():
    assert get_mcp_entry(EXECUTABLE) == {
        "command": EXECUTABLE,
        "args": ["serve"],
    }


def test_generated_agent_tool_names_match_current_mcp_surface():
    from agent_eyes.server import TOOLS

    expected = {
        f"mcp__agent-eyes__{tool.name}"
        for tool in TOOLS
        if tool.name != "install_check"
    }
    assert set(AGENT_EYES_TOOLS) == expected
    assert get_mcp_entry_zed(EXECUTABLE) == {
        "source": "custom",
        "command": EXECUTABLE,
        "args": ["serve"],
    }


def test_generated_agent_tool_names_follow_platform_specific_surface():
    macos_tools = set(_agent_eyes_tools_for_platform("darwin"))
    linux_tools = set(_agent_eyes_tools_for_platform("linux"))
    windows_tools = set(_agent_eyes_tools_for_platform("win32"))

    assert {
        "mcp__agent-eyes__app",
        "mcp__agent-eyes__window",
    } <= macos_tools
    assert "mcp__agent-eyes__app" not in linux_tools
    assert "mcp__agent-eyes__window" not in linux_tools
    assert "mcp__agent-eyes__app" not in windows_tools
    assert "mcp__agent-eyes__window" not in windows_tools


def test_configure_preserves_unrelated_servers_and_creates_backup(tmp_path):
    path = tmp_path / "mcp.json"
    original = {
        "theme": "dark",
        "mcpServers": {"other": {"command": "other-server"}},
    }
    path.write_text(json.dumps(original))

    result = configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    configured = json.loads(path.read_text())
    assert configured["theme"] == "dark"
    assert configured["mcpServers"]["other"] == {"command": "other-server"}
    assert configured["mcpServers"]["agent-eyes"]["args"] == ["serve"]
    assert result.changed is True
    assert result.backup is not None
    assert Path(result.backup).exists()


def test_strict_json_updates_only_agent_eyes_and_preserves_unrelated_formatting(tmp_path):
    path = tmp_path / "mcp.json"
    original = """{
  "theme" : "dark",
  "mcpServers" : {"other" : {"command" : "other"}}
}
"""
    path.write_text(original)

    configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert '  "theme" : "dark",' in configured
    assert '"other" : {"command" : "other"}' in configured
    assert json.loads(configured)["mcpServers"]["agent-eyes"] == get_mcp_entry(
        EXECUTABLE
    )


def test_configure_is_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"agent-eyes": get_mcp_entry(EXECUTABLE)}}))

    result = configure_mcp_file(path, servers_key="mcpServers", executable=EXECUTABLE)

    assert result.changed is False
    assert result.backup is None


def test_invalid_json_fails_closed(tmp_path):
    path = tmp_path / "mcp.json"
    invalid = "{broken user config"
    path.write_text(invalid)

    with pytest.raises(InvalidConfigError):
        configure_mcp_file(path, servers_key="mcpServers", executable=EXECUTABLE)

    assert path.read_text() == invalid


@pytest.mark.parametrize("constant", ["Infinity", "-Infinity", "NaN"])
@pytest.mark.parametrize("config_format", ["json", "jsonc"])
def test_nonstandard_json_numeric_constants_fail_closed(
    tmp_path, constant, config_format
):
    path = tmp_path / f"mcp.{config_format}"
    original = f'{{"threshold": {constant}, "mcpServers": {{}}}}'
    path.write_text(original)

    with pytest.raises(InvalidConfigError, match="Invalid JSON"):
        configure_mcp_file(
            path,
            servers_key="mcpServers",
            executable=EXECUTABLE,
            config_format=config_format,
            backups_dir=tmp_path / "backups",
        )

    assert path.read_text() == original
    assert not (tmp_path / "backups").exists()


def test_dry_run_does_not_touch_config(tmp_path):
    path = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {}})
    path.write_text(original)

    result = configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        dry_run=True,
    )

    assert result.changed is True
    assert result.applied is False
    assert path.read_text() == original


def test_configure_preserves_symlink_and_writes_its_target(tmp_path):
    target = tmp_path / "shared" / "mcp.json"
    target.parent.mkdir()
    original = json.dumps({"mcpServers": {"other": {"command": "other"}}})
    target.write_text(original)
    path = tmp_path / "mcp.json"
    path.symlink_to(target)

    result = configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    assert path.is_symlink()
    assert json.loads(target.read_text())["mcpServers"]["agent-eyes"] == get_mcp_entry(
        EXECUTABLE
    )
    assert result.path == str(path.absolute())
    assert Path(result.backup).read_text() == original


def test_configure_preserves_relative_symlink_and_writes_its_target(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {}}))
    path = tmp_path / "mcp.json"
    path.symlink_to(Path("shared") / "mcp.json")

    configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    assert path.is_symlink()
    assert path.readlink() == Path("shared") / "mcp.json"
    assert json.loads(target.read_text())["mcpServers"]["agent-eyes"] == (
        get_mcp_entry(EXECUTABLE)
    )


def test_atomic_config_update_preserves_extended_attributes(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    attribute = "com.agent-eyes.audit" if sys.platform == "darwin" else "user.agent-eyes.audit"
    if sys.platform == "darwin" and Path("/usr/bin/xattr").is_file():
        subprocess.run(
            ["/usr/bin/xattr", "-w", attribute, "retain-me", str(path)],
            check=True,
        )

        def read_attribute() -> bytes:
            return subprocess.run(
                ["/usr/bin/xattr", "-p", attribute, str(path)],
                check=True,
                capture_output=True,
            ).stdout.rstrip(b"\n")
    elif all(hasattr(os, name) for name in ("setxattr", "getxattr")):
        try:
            os.setxattr(path, attribute, b"retain-me")
        except OSError as exc:
            pytest.skip(f"test filesystem does not support extended attributes: {exc}")

        def read_attribute() -> bytes:
            return os.getxattr(path, attribute)
    else:
        pytest.skip("extended attributes are unavailable")

    configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    assert read_attribute() == b"retain-me"


def test_atomic_config_update_preserves_macos_acl_entries(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS ACL contract")
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    acl_entry = "group:everyone allow read"
    subprocess.run(["/bin/chmod", "+a", acl_entry, str(path)], check=True)

    configure_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    acl = subprocess.run(
        ["/bin/ls", "-lde", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert acl_entry in acl


def test_config_symlink_to_non_file_target_fails_closed(tmp_path):
    target = tmp_path / "directory"
    target.mkdir()
    path = tmp_path / "mcp.json"
    path.symlink_to(target)

    with pytest.raises(InvalidConfigError, match="regular file"):
        configure_mcp_file(
            path,
            servers_key="mcpServers",
            executable=EXECUTABLE,
            backups_dir=tmp_path / "backups",
        )

    assert path.is_symlink()
    assert not (tmp_path / "backups").exists()


def test_preflight_renders_without_writing_and_apply_uses_same_plan(tmp_path):
    path = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {}})
    path.write_text(original)

    plan = preflight_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )

    assert plan.changed is True
    assert plan.path == str(path.absolute())
    assert plan.write_path == str(path.absolute())
    assert plan.original_content == original
    assert json.loads(plan.rendered_content)["mcpServers"]["agent-eyes"] == (
        get_mcp_entry(EXECUTABLE)
    )
    assert path.read_text() == original

    result = apply_mcp_plan(plan, backups_dir=tmp_path / "backups")

    assert result.applied is True
    assert path.read_text() == plan.rendered_content


def test_apply_rejects_config_changed_after_preflight(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    plan = preflight_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    changed_by_user = json.dumps({"mcpServers": {}, "userChange": True})
    path.write_text(changed_by_user)

    with pytest.raises(InvalidConfigError, match="changed since preflight"):
        apply_mcp_plan(plan)

    assert path.read_text() == changed_by_user


def test_apply_rejects_edit_injected_at_the_atomic_write_boundary(
    tmp_path, monkeypatch
):
    path = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {}})
    path.write_text(original)
    plan = preflight_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    changed_by_user = json.dumps({"mcpServers": {}, "userChange": True})
    real_atomic_write = configurator._atomic_write
    injected = False

    def inject_user_edit(write_path: Path, content: str, **kwargs):
        nonlocal injected
        if write_path == path and not injected:
            injected = True
            path.write_text(changed_by_user)
        return real_atomic_write(write_path, content, **kwargs)

    monkeypatch.setattr(configurator, "_atomic_write", inject_user_edit)

    with pytest.raises(InvalidConfigError, match="changed while setup"):
        apply_mcp_plan(
            plan,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert path.read_text() == changed_by_user


def test_apply_rejects_symlink_retargeted_after_preflight(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    original = json.dumps({"mcpServers": {}})
    first.write_text(original)
    second.write_text(original)
    path = tmp_path / "mcp.json"
    path.symlink_to(first)
    plan = preflight_mcp_file(
        path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    path.unlink()
    path.symlink_to(second)

    with pytest.raises(InvalidConfigError, match="changed since preflight"):
        apply_mcp_plan(plan)

    assert path.is_symlink()
    assert first.read_text() == original
    assert second.read_text() == original


def test_apply_rejects_parent_directory_symlink_retargeted_after_preflight(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    original = json.dumps({"mcpServers": {}})
    first = first_dir / "mcp.json"
    second = second_dir / "mcp.json"
    first.write_text(original)
    second.write_text(original)
    selected = tmp_path / "selected"
    selected.symlink_to(first_dir, target_is_directory=True)
    logical_path = selected / "mcp.json"
    plan = preflight_mcp_file(
        logical_path,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    selected.unlink()
    selected.symlink_to(second_dir, target_is_directory=True)

    with pytest.raises(InvalidConfigError, match="changed since preflight"):
        apply_mcp_plan(
            plan,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert first.read_text() == original
    assert second.read_text() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor contract")
def test_parent_retarget_during_write_cannot_redirect_mutation_outside(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / ".client"
    config_dir.mkdir()
    config = config_dir / "mcp.json"
    original = json.dumps({"mcpServers": {}})
    config.write_text(original)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "mcp.json"
    outside.write_text(original)
    plan = preflight_mcp_file(
        config,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    real_create_temporary = configurator._create_temporary_at
    moved_dir = project / ".client-original"
    retargeted = False

    def retarget_parent(parent_descriptor: int, path: Path):
        nonlocal retargeted
        created = real_create_temporary(parent_descriptor, path)
        if not retargeted:
            retargeted = True
            config_dir.rename(moved_dir)
            config_dir.symlink_to(outside_dir, target_is_directory=True)
        return created

    monkeypatch.setattr(configurator, "_create_temporary_at", retarget_parent)

    with pytest.raises(InvalidConfigError, match="parent changed"):
        apply_mcp_plan(
            plan,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert outside.read_text() == original
    assert (moved_dir / "mcp.json").read_text() == original
    assert not list(moved_dir.glob("*.tmp"))


def test_multiple_clients_can_all_be_preflighted_before_any_write(tmp_path):
    valid = tmp_path / "valid.json"
    valid_content = json.dumps({"mcpServers": {}})
    valid.write_text(valid_content)
    corrupt = tmp_path / "corrupt.json"
    corrupt_content = "{not valid"
    corrupt.write_text(corrupt_content)

    plan = preflight_mcp_file(
        valid,
        servers_key="mcpServers",
        executable=EXECUTABLE,
    )
    with pytest.raises(InvalidConfigError):
        preflight_mcp_file(
            corrupt,
            servers_key="mcpServers",
            executable=EXECUTABLE,
        )

    assert plan.changed is True
    assert valid.read_text() == valid_content
    assert corrupt.read_text() == corrupt_content


def test_vscode_jsonc_comments_and_trailing_commas_are_preserved(tmp_path):
    path = tmp_path / "mcp.json"
    original = """{
  // Keep the user's top-level note.
  "servers": {
    "other": {
      "command": "other",
    }, // Keep the other server note.
  },
}
"""
    path.write_text(original)

    result = configure_mcp_file(
        path,
        servers_key="servers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert result.applied is True
    assert "// Keep the user's top-level note." in configured
    assert "// Keep the other server note." in configured
    assert f'"command": {json.dumps(EXECUTABLE)}' in configured
    assert preflight_mcp_file(
        path,
        servers_key="servers",
        executable=EXECUTABLE,
    ).changed is False


def test_zed_jsonc_is_updated_without_reformatting_unrelated_content(tmp_path):
    path = tmp_path / "settings.json"
    original = """{
\t// Zed settings can be JSONC.
\t"theme": "Ayu Dark",
\t"context_servers": {
\t\t// Existing context server.
\t\t"other": { "source": "extension" },
\t},
}
"""
    path.write_text(original)

    configure_mcp_file(
        path,
        servers_key="context_servers",
        executable=EXECUTABLE,
        is_zed=True,
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert "\t// Zed settings can be JSONC." in configured
    assert "\t\t// Existing context server." in configured
    assert '\t"theme": "Ayu Dark",' in configured
    assert '"source": "custom"' in configured
    assert preflight_mcp_file(
        path,
        servers_key="context_servers",
        executable=EXECUTABLE,
        is_zed=True,
    ).changed is False


def test_jsonc_replaces_only_existing_agent_eyes_value(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        """{
  // Keep root note.
  "servers": {
    "other": { "command": "other" }, // Keep server note.
    "agent-eyes": {
      "command": "old",
      "args": ["old"],
    },
  },
}
"""
    )

    configure_mcp_file(
        path,
        servers_key="servers",
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert "// Keep root note." in configured
    assert "// Keep server note." in configured
    assert '"command": "old"' not in configured
    assert f'"command": {json.dumps(EXECUTABLE)}' in configured
    assert preflight_mcp_file(
        path,
        servers_key="servers",
        executable=EXECUTABLE,
    ).changed is False


@pytest.mark.parametrize(
    "content",
    [
        '{\n  // comment\n  "servers": {\n',
        '{\n  /* unterminated comment\n  "servers": {}\n}',
    ],
)
def test_corrupt_jsonc_fails_closed(tmp_path, content):
    path = tmp_path / "mcp.json"
    path.write_text(content)

    with pytest.raises(InvalidConfigError, match="Invalid JSON/JSONC config"):
        configure_mcp_file(path, servers_key="servers", executable=EXECUTABLE)

    assert path.read_text() == content


def test_codex_toml_replaces_only_agent_eyes_table(tmp_path):
    path = tmp_path / "config.toml"
    original = """# Keep the user's top-level note.
model = "gpt-5"

[mcp_servers.other]
command = "other"
args = ["--keep"]

[mcp_servers.agent-eyes]
command = "old"
args = ["old"]

[features]
keep_me = true
"""
    path.write_text(original)

    result = configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert result.applied is True
    assert "# Keep the user's top-level note." in configured
    assert '[mcp_servers.other]\ncommand = "other"\nargs = ["--keep"]' in configured
    assert "[features]\nkeep_me = true" in configured
    assert f"command = {json.dumps(EXECUTABLE)}" in configured
    assert 'args = ["serve"]' in configured
    assert 'command = "old"' not in configured
    assert preflight_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
    ).changed is False


def test_codex_toml_removes_only_agent_eyes_descendant_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """[mcp_servers.agent-eyes]
command = "old"

[mcp_servers.other]
command = "other"

[mcp_servers.agent-eyes.env]
SECRET = "must-not-survive-the-replacement"

[unrelated]
value = 1
"""
    )

    configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert "must-not-survive-the-replacement" not in configured
    assert '[mcp_servers.other]\ncommand = "other"' in configured
    assert "[unrelated]\nvalue = 1" in configured


def test_toml_replacement_preserves_comments_before_the_next_table(tmp_path):
    path = tmp_path / "config.toml"
    sentinel = "# SENTINEL: documents the following unrelated server"
    content = (
        '[mcp_servers.agent-eyes]\ncommand = "old"\nargs = ["old"]\n'
        f"{sentinel}\n"
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    path.write_text(content)

    configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    assert sentinel in path.read_text()
    assert configurator._toml.loads(path.read_text())["mcp_servers"]["other"] == {
        "command": "other"
    }


def test_toml_header_text_inside_multiline_string_is_not_treated_as_a_table(tmp_path):
    path = tmp_path / "config.toml"
    original = '''message = """
[mcp_servers.agent-eyes]
This is documentation, not a table.
"""

[mcp_servers.other]
command = "other"
'''
    path.write_text(original)

    configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert 'message = """\n[mcp_servers.agent-eyes]\n' in configured
    assert "This is documentation, not a table." in configured
    assert configured.count("[mcp_servers.agent-eyes]") == 2


def test_new_toml_entry_preserves_crlf_line_endings(tmp_path):
    path = tmp_path / "config.toml"
    original = '[mcp_servers.other]\r\ncommand = "other"\r\n'
    path.write_bytes(original.encode())

    configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_bytes()
    assert b"\r\n[mcp_servers.agent-eyes]\r\n" in configured
    assert configured.replace(b"\r\n", b"").find(b"\n") == -1


def test_toml_with_unrelated_nan_value_can_be_updated_exactly(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('sampling_threshold = nan\n\n[mcp_servers.other]\ncommand = "other"\n')

    configure_mcp_file(
        path,
        servers_key="mcp_servers",
        executable=EXECUTABLE,
        config_format="toml",
        backups_dir=tmp_path / "backups",
    )

    configured = path.read_text()
    assert "sampling_threshold = nan" in configured
    assert "[mcp_servers.agent-eyes]" in configured


@pytest.mark.parametrize(
    "content",
    [
        '[mcp_servers.agent-eyes\ncommand = "broken"\n',
        '[mcp_servers.agent-eyes]\ncommand = "unterminated\n',
    ],
)
def test_malformed_toml_fails_closed_without_backup_or_write(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    backups = tmp_path / "backups"

    with pytest.raises(InvalidConfigError, match="Invalid TOML config"):
        configure_mcp_file(
            path,
            servers_key="mcp_servers",
            executable=EXECUTABLE,
            config_format="toml",
            backups_dir=backups,
        )

    assert path.read_text() == content
    assert not backups.exists()


def test_missing_toml_parser_fails_closed_without_write(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    content = '[mcp_servers.other]\ncommand = "other"\n'
    path.write_text(content)
    monkeypatch.setattr(configurator, "_toml", None)

    with pytest.raises(InvalidConfigError, match="TOML validation is unavailable"):
        configure_mcp_file(
            path,
            servers_key="mcp_servers",
            executable=EXECUTABLE,
            config_format="toml",
            backups_dir=tmp_path / "backups",
        )

    assert path.read_text() == content
    assert not (tmp_path / "backups").exists()


def test_toml_inline_agent_eyes_entry_fails_closed_instead_of_broad_rewrite(tmp_path):
    path = tmp_path / "config.toml"
    content = (
        '[mcp_servers]\nagent-eyes = { command = "old", args = ["old"] }\n'
        '[unrelated]\nvalue = true\n'
    )
    path.write_text(content)

    with pytest.raises(InvalidConfigError, match="table form"):
        configure_mcp_file(
            path,
            servers_key="mcp_servers",
            executable=EXECUTABLE,
            config_format="toml",
        )

    assert path.read_text() == content


def test_group_apply_rolls_back_prior_file_if_later_atomic_write_fails(
    tmp_path, monkeypatch
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    original_first = json.dumps({"mcpServers": {"first": {"command": "one"}}})
    original_second = json.dumps({"mcpServers": {"second": {"command": "two"}}})
    first.write_text(original_first)
    second.write_text(original_second)
    plans = (
        preflight_mcp_file(first, servers_key="mcpServers", executable=EXECUTABLE),
        preflight_mcp_file(second, servers_key="mcpServers", executable=EXECUTABLE),
    )
    real_atomic_write = configurator._atomic_write
    failed = False

    def fail_second_once(path: Path, content: str, **kwargs):
        nonlocal failed
        if path == second and not failed:
            failed = True
            raise OSError("injected second-write failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(configurator, "_atomic_write", fail_second_once)

    with pytest.raises(OSError, match="injected second-write failure"):
        configurator.apply_mcp_plans(
            plans,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert first.read_text() == original_first
    assert second.read_text() == original_second


def test_group_rollback_never_overwrites_an_external_post_write_edit(
    tmp_path, monkeypatch
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    original_first = json.dumps({"mcpServers": {}})
    original_second = json.dumps({"mcpServers": {}})
    first.write_text(original_first)
    second.write_text(original_second)
    plans = (
        preflight_mcp_file(first, servers_key="mcpServers", executable=EXECUTABLE),
        preflight_mcp_file(second, servers_key="mcpServers", executable=EXECUTABLE),
    )
    external_edit = json.dumps({"external": "preserve"})
    real_atomic_write = configurator._atomic_write

    def fail_second_after_external_edit(path: Path, content: str, **kwargs):
        if path == second:
            first.write_text(external_edit)
            raise OSError("injected second-write failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(configurator, "_atomic_write", fail_second_after_external_edit)

    with pytest.raises(InvalidConfigError, match="rollback was incomplete"):
        configurator.apply_mcp_plans(
            plans,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert first.read_text() == external_edit
    assert second.read_text() == original_second


def test_group_rollback_restores_pinned_target_after_logical_symlink_retarget(
    tmp_path, monkeypatch
):
    first_target = tmp_path / "first-target.json"
    retargeted = tmp_path / "retargeted.json"
    second = tmp_path / "second.json"
    original = json.dumps({"mcpServers": {}})
    first_target.write_text(original)
    retargeted.write_text(original)
    second.write_text(original)
    logical = tmp_path / "first.json"
    logical.symlink_to(first_target)
    plans = (
        preflight_mcp_file(logical, servers_key="mcpServers", executable=EXECUTABLE),
        preflight_mcp_file(second, servers_key="mcpServers", executable=EXECUTABLE),
    )
    real_atomic_write = configurator._atomic_write

    def fail_second_after_retarget(path: Path, content: str, **kwargs):
        if path == second:
            logical.unlink()
            logical.symlink_to(retargeted)
            raise OSError("injected second-write failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(configurator, "_atomic_write", fail_second_after_retarget)

    with pytest.raises(OSError, match="injected second-write failure"):
        configurator.apply_mcp_plans(
            plans,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert first_target.read_text() == original
    assert retargeted.read_text() == original
    assert second.read_text() == original


def test_concurrent_processes_apply_the_same_plan_once_and_remain_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    plan = preflight_mcp_file(path, servers_key="mcpServers", executable=EXECUTABLE)
    backups = tmp_path / "backups"
    lock_path = tmp_path / ".setup.lock"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_apply_plan_in_process,
            args=(plan, str(backups), str(lock_path), queue),
        )
        for _ in range(2)
    ]

    started_processes = []
    try:
        for process in processes:
            process.start()
            started_processes.append(process)
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0

        assert all(error is None for _, _, _, error in results)
        assert sum(applied for applied, _, _, _ in results) == 1
        assert json.loads(path.read_text())["mcpServers"][
            "agent-eyes"
        ] == get_mcp_entry(EXECUTABLE)
        assert len(list(backups.glob("*.bak"))) == 1
    finally:
        for process in started_processes:
            if process.is_alive():
                process.terminate()
        for process in started_processes:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            if not process.is_alive():
                process.close()
        queue.close()
        queue.join_thread()


def test_backups_are_private_and_unique(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("first")
    backups = tmp_path / "backups"

    first = configurator._backup(path, backups)
    path.write_text("second")
    second = configurator._backup(path, backups)

    assert first != second
    assert Path(first).read_text() == "first"
    assert Path(second).read_text() == "second"
    if os.name != "nt":
        assert backups.stat().st_mode & 0o777 == 0o700
        assert Path(first).stat().st_mode & 0o777 == 0o600


def test_default_backup_path_rejects_a_symlinked_state_directory(
    tmp_path, monkeypatch
):
    target = tmp_path / "state-target"
    target.mkdir(mode=0o755)
    original_mode = target.stat().st_mode & 0o777
    state_link = tmp_path / "state-link"
    state_link.symlink_to(target, target_is_directory=True)
    source = tmp_path / "mcp.json"
    source.write_text("{}")
    monkeypatch.setenv("AGENT_EYES_STATE_DIR", str(state_link))

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        configurator._backup(source)

    assert target.stat().st_mode & 0o777 == original_mode
    assert list(target.iterdir()) == []


def test_apply_setup_preflights_every_selected_client_before_any_write(
    tmp_path, monkeypatch
):
    valid = tmp_path / "valid.json"
    invalid = tmp_path / "invalid.toml"
    original_valid = json.dumps({"mcpServers": {}})
    original_invalid = '[mcp_servers.agent-eyes\ncommand = "broken"\n'
    valid.write_text(original_valid)
    invalid.write_text(original_invalid)
    definitions = [
        {
            "id": "valid",
            "config_locations": {
                "global_mcp": {
                    "path": valid,
                    "key": "mcpServers",
                    "format": "json",
                }
            },
        },
        {
            "id": "invalid",
            "config_locations": {
                "global_mcp": {
                    "path": invalid,
                    "key": "mcp_servers",
                    "format": "toml",
                }
            },
        },
    ]
    monkeypatch.setattr(configurator, "_ai_tool_definitions", lambda: definitions)

    with pytest.raises(InvalidConfigError, match="Invalid TOML config"):
        configurator.apply_setup(
            replace_competitors=[],
            configure_tools=["valid", "invalid"],
            executable=EXECUTABLE,
            backups_dir=tmp_path / "backups",
            lock_path=tmp_path / ".setup.lock",
        )

    assert valid.read_text() == original_valid
    assert invalid.read_text() == original_invalid
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / ".setup.lock").exists()


def test_normal_setup_never_removes_competitors_or_rewrites_instructions(
    tmp_path, monkeypatch
):
    path = tmp_path / "claude.json"
    competitor = {"command": "keep-this-server"}
    path.write_text(json.dumps({"mcpServers": {"playwright": competitor}}))
    definitions = [
        {
            "id": "claude-code",
            "config_locations": {
                "global_mcp": {
                    "path": path,
                    "key": "mcpServers",
                    "format": "json",
                }
            },
            "supports_skills": True,
            "supports_agents": True,
        }
    ]
    monkeypatch.setattr(configurator, "_ai_tool_definitions", lambda: definitions)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("normal setup called a quarantined broad mutation")

    monkeypatch.setattr(configurator, "_remove_competitor_from_mcp", forbidden)
    monkeypatch.setattr(configurator, "_update_claude_md", forbidden)
    monkeypatch.setattr(configurator, "_update_agent_definitions", forbidden)
    monkeypatch.setattr(configurator, "_install_skill", forbidden)

    result = configurator.apply_setup(
        replace_competitors=["playwright-mcp"],
        configure_tools=["claude-code"],
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
        lock_path=tmp_path / ".setup.lock",
        consent=True,
        scan_report={
            "by_tool": {
                "claude-code": {
                    "mcp_competitors": [
                        {
                            "competitor_id": "playwright-mcp",
                            "config_path": str(path),
                            "server_key": "playwright",
                        }
                    ]
                }
            }
        },
    )

    configured = json.loads(path.read_text())
    assert configured["mcpServers"]["playwright"] == competitor
    assert configured["mcpServers"]["agent-eyes"] == get_mcp_entry(EXECUTABLE)
    assert any("not removed" in warning for warning in result["warnings"])


def test_project_setup_rejects_a_config_symlink_outside_the_project(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    original = json.dumps({"mcpServers": {}})
    outside.write_text(original)
    config_dir = project / ".client"
    config_dir.mkdir()
    logical_path = config_dir / "mcp.json"
    logical_path.symlink_to(outside)
    definitions = [
        {
            "id": "client",
            "config_locations": {
                "project_mcp": {
                    "path": Path(".client/mcp.json"),
                    "key": "mcpServers",
                    "format": "json",
                }
            },
        }
    ]
    monkeypatch.chdir(project)
    monkeypatch.setattr(configurator, "_ai_tool_definitions", lambda: definitions)

    with pytest.raises(InvalidConfigError, match="outside the project"):
        configurator.apply_setup(
            replace_competitors=[],
            configure_tools=["client"],
            level="project",
            executable=EXECUTABLE,
            backups_dir=project / "backups",
            lock_path=project / ".setup.lock",
            consent=True,
        )

    assert outside.read_text() == original
    assert not (project / "backups").exists()
    assert not (project / ".setup.lock").exists()


@pytest.mark.parametrize("mode", ["dry_run", "declined"])
def test_setup_preview_and_decline_create_no_files_or_directories(
    tmp_path, monkeypatch, mode
):
    path = tmp_path / "new" / "mcp.json"
    definitions = [
        {
            "id": "cursor",
            "config_locations": {
                "global_mcp": {
                    "path": path,
                    "key": "mcpServers",
                    "format": "json",
                }
            },
        }
    ]
    monkeypatch.setattr(configurator, "_ai_tool_definitions", lambda: definitions)

    result = configurator.apply_setup(
        replace_competitors=[],
        configure_tools=["cursor"],
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
        lock_path=tmp_path / ".setup.lock",
        dry_run=mode == "dry_run",
        consent=mode != "declined",
    )

    assert result["applied"] is False
    assert result["cancelled"] is (mode == "declined")
    assert not path.parent.exists()
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / ".setup.lock").exists()


def test_setup_apply_defaults_to_cancelled_without_explicit_consent(
    tmp_path, monkeypatch
):
    path = tmp_path / "new" / "mcp.json"
    definitions = [
        {
            "id": "cursor",
            "config_locations": {
                "global_mcp": {
                    "path": path,
                    "key": "mcpServers",
                    "format": "json",
                }
            },
        }
    ]
    monkeypatch.setattr(configurator, "_ai_tool_definitions", lambda: definitions)

    result = configurator.apply_setup(
        replace_competitors=[],
        configure_tools=["cursor"],
        executable=EXECUTABLE,
        backups_dir=tmp_path / "backups",
        lock_path=tmp_path / ".setup.lock",
    )

    assert result["applied"] is False
    assert result["cancelled"] is True
    assert not path.parent.exists()
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / ".setup.lock").exists()
