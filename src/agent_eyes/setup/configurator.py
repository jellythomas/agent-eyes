"""Apply agent-eyes configuration to selected AI tools.

CRITICAL: This module modifies user config files. Every change:
1. Creates a backup first
2. Only touches the specific sections needed
3. Preserves all unrelated config
4. Reports exactly what changed
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .state import get_backups_dir
from .templates.mcp_entry import (
    get_mcp_entry,
    get_mcp_entry_zed,
    get_agent_eyes_tools_list,
    COMPETITOR_TOOL_PATTERNS,
)
from .templates.skill import SKILL_MD
from .templates.claude_md import CLAUDE_MD_SECTION
from .scanner import _ai_tool_definitions


def _backup(path: Path) -> str | None:
    """Create a timestamped backup of a file. Returns backup path or None."""
    if not path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = get_backups_dir()
    # Preserve directory structure in backup
    safe_name = str(path).replace("/", "__").replace("\\", "__")
    backup_path = backup_dir / f"{safe_name}.{ts}.bak"
    shutil.copy2(path, backup_path)
    return str(backup_path)


def _read_json(path: Path) -> dict | None:
    """Safely read a JSON file."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict) -> None:
    """Write JSON with consistent formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ── MCP Config Modification ─────────────────────────────────────────

def _add_agent_eyes_to_mcp(
    config_path: Path,
    servers_key: str,
    is_zed: bool = False,
) -> dict:
    """Add agent-eyes MCP entry to a config file.

    Returns action report.
    """
    backup = _backup(config_path)
    data = _read_json(config_path) or {}

    if servers_key not in data:
        data[servers_key] = {}

    entry = get_mcp_entry_zed() if is_zed else get_mcp_entry()
    data[servers_key]["agent-eyes"] = entry
    _write_json(config_path, data)

    return {
        "action": "added_mcp_entry",
        "path": str(config_path),
        "backup": backup,
        "detail": "Added agent-eyes MCP server entry",
    }


def _remove_competitor_from_mcp(
    config_path: Path,
    servers_key: str,
    server_key_to_remove: str,
) -> dict | None:
    """Remove a specific competitor server entry from MCP config.

    Returns action report or None if nothing to do.
    """
    data = _read_json(config_path)
    if not data:
        return None

    servers = data.get(servers_key, {})
    if server_key_to_remove not in servers:
        return None

    backup = _backup(config_path)
    del data[servers_key][server_key_to_remove]
    _write_json(config_path, data)

    return {
        "action": "removed_competitor",
        "path": str(config_path),
        "backup": backup,
        "detail": f"Removed '{server_key_to_remove}' from {servers_key}",
    }


# ── Claude Code Skill Installation ──────────────────────────────────

def _install_skill(level: str) -> dict:
    """Install agent-eyes SKILL.md at the specified level.

    Levels:
      - "global": ~/.claude/skills/agent-eyes/SKILL.md
      - "project": .claude/skills/agent-eyes/SKILL.md (cwd)
    """
    if level == "global":
        skill_dir = Path.home() / ".claude" / "skills" / "agent-eyes"
    else:
        skill_dir = Path.cwd() / ".claude" / "skills" / "agent-eyes"

    skill_file = skill_dir / "SKILL.md"
    backup = _backup(skill_file)

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(SKILL_MD)

    return {
        "action": "installed_skill",
        "path": str(skill_file),
        "backup": backup,
        "detail": f"Installed agent-eyes SKILL.md ({level} level)",
    }


# ── Claude Code CLAUDE.md Update ────────────────────────────────────

def _update_claude_md(level: str) -> dict:
    """Add agent-eyes priority section to CLAUDE.md.

    Only adds if not already present. Preserves existing content.

    Levels:
      - "global": ~/.claude/CLAUDE.md
      - "project": ./CLAUDE.md (cwd)
    """
    if level == "global":
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
    else:
        claude_md = Path.cwd() / "CLAUDE.md"

    existing = ""
    if claude_md.exists():
        existing = claude_md.read_text()

    # Check if already has agent-eyes section
    if "agent-eyes Priority" in existing:
        return {
            "action": "skipped_claude_md",
            "path": str(claude_md),
            "backup": None,
            "detail": "CLAUDE.md already has agent-eyes section",
        }

    backup = _backup(claude_md)

    # Append section
    new_content = existing.rstrip() + "\n\n" + CLAUDE_MD_SECTION + "\n"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(new_content)

    return {
        "action": "updated_claude_md",
        "path": str(claude_md),
        "backup": backup,
        "detail": f"Added agent-eyes priority section to CLAUDE.md ({level} level)",
    }


# ── Claude Code Agent Definition Update ─────────────────────────────

def _update_agent_definitions(
    agents_dir: Path,
    competitor_ids: list[str],
) -> list[dict]:
    """Replace competitor tool references in agent .md files.

    For each agent file that references a competitor's tools, replace those
    tool references with agent-eyes equivalents.

    CAREFUL: Only replaces the specific competitor tool patterns, preserves
    everything else in the file.
    """
    results = []
    if not agents_dir.exists():
        return results

    ae_tools = get_agent_eyes_tools_list()

    for agent_file in agents_dir.glob("*.md"):
        try:
            content = agent_file.read_text()
        except OSError:
            continue

        modified = False
        for comp_id in competitor_ids:
            pattern = COMPETITOR_TOOL_PATTERNS.get(comp_id)
            if not pattern:
                continue

            # Find all competitor tool references (e.g., mcp__playwright__browser_click)
            matches = re.findall(pattern, content)
            if not matches:
                continue

            # Replace the entire block of competitor tools with agent-eyes tools
            # Strategy: find the contiguous sequence of competitor tools and replace
            # with the agent-eyes tools list.
            #
            # Competitor tools appear as comma-separated in the `tools:` frontmatter.
            # We replace each individual tool ref, then deduplicate the agent-eyes
            # tools that got inserted multiple times.

            backup = _backup(agent_file)

            # Replace all individual tool refs with a placeholder
            placeholder = "<<AGENT_EYES_TOOLS>>"
            new_content = content
            for match in matches:
                new_content = new_content.replace(match, placeholder)

            # Now collapse multiple adjacent placeholders (with commas/spaces between)
            # into a single agent-eyes tools list
            collapse_pattern = r"(?:<<AGENT_EYES_TOOLS>>(?:\s*,\s*)?)+<<AGENT_EYES_TOOLS>>"
            while re.search(collapse_pattern, new_content):
                new_content = re.sub(collapse_pattern, placeholder, new_content)

            # Replace the remaining single placeholder with actual tools
            new_content = new_content.replace(placeholder, ae_tools)

            # Clean up any resulting double commas or trailing commas
            new_content = re.sub(r",\s*,", ",", new_content)
            new_content = re.sub(r",\s*$", "", new_content, flags=re.MULTILINE)

            agent_file.write_text(new_content)
            modified = True

            results.append({
                "action": "updated_agent",
                "path": str(agent_file),
                "backup": backup,
                "detail": (
                    f"Replaced {len(matches)} {comp_id} tool refs "
                    f"with agent-eyes tools in {agent_file.name}"
                ),
            })

    return results


# ── Main Apply Function ─────────────────────────────────────────────

def apply_setup(
    replace_competitors: list[str],
    configure_tools: list[str],
    level: str = "global",
    scan_report: dict | None = None,
) -> dict:
    """Apply agent-eyes configuration based on user selections.

    Args:
        replace_competitors: List of competitor IDs to replace
            (e.g., ["playwright-mcp", "puppeteer-mcp"])
        configure_tools: List of AI tool IDs to configure
            (e.g., ["claude-code", "cursor"])
        level: Installation level — "global" or "project"
        scan_report: The scan report from scan_competitors() for
            looking up config paths and server keys

    Returns:
        Dict with changes made, warnings, and backup locations.
    """
    changes: list[dict] = []
    warnings: list[str] = []

    # Build tool definition lookup
    tool_defs = {d["id"]: d for d in _ai_tool_definitions()}

    for tool_id in configure_tools:
        defn = tool_defs.get(tool_id)
        if not defn:
            warnings.append(f"Unknown AI tool: {tool_id}")
            continue

        # ── 1. Add agent-eyes MCP entry ──────────────────────────────
        mcp_loc = defn["config_locations"].get("global_mcp")
        if mcp_loc and mcp_loc.get("format") == "json":
            path = Path(mcp_loc["path"])
            if not str(path).startswith("."):  # Skip relative paths
                is_zed = tool_id == "zed"
                result = _add_agent_eyes_to_mcp(path, mcp_loc["key"], is_zed)
                changes.append({"tool": tool_id, **result})

        # ── 2. Remove selected competitors from MCP config ───────────
        if scan_report:
            tool_report = scan_report.get("by_tool", {}).get(tool_id, {})
            for comp in tool_report.get("mcp_competitors", []):
                if comp["competitor_id"] in replace_competitors:
                    result = _remove_competitor_from_mcp(
                        Path(comp["config_path"]),
                        mcp_loc["key"] if mcp_loc else "mcpServers",
                        comp["server_key"],
                    )
                    if result:
                        changes.append({"tool": tool_id, **result})

        # ── 3. Claude Code specific: skill + CLAUDE.md + agents ──────
        if tool_id == "claude-code":
            # Install skill
            if defn.get("supports_skills"):
                result = _install_skill(level)
                changes.append({"tool": tool_id, **result})

            # Update CLAUDE.md
            result = _update_claude_md(level)
            changes.append({"tool": tool_id, **result})

            # Update agent definitions
            if defn.get("supports_agents") and replace_competitors:
                agents_dir = Path(defn["config_locations"]["agents"]["path"])
                agent_results = _update_agent_definitions(
                    agents_dir, replace_competitors
                )
                for r in agent_results:
                    changes.append({"tool": tool_id, **r})

    # ── 4. Remove competing skills from Claude Code ──────────────────
    if "claude-code" in configure_tools:
        home_skills = Path.home() / ".claude" / "skills"
        competing_skill_dirs = [
            "playwright-skill", "playwright", "puppeteer-skill", "puppeteer",
            "browser-automation",
        ]
        for comp_id in replace_competitors:
            if comp_id.startswith("skill:"):
                dirname = comp_id.replace("skill:", "")
                competing_skill_dirs.append(dirname)

        for dirname in competing_skill_dirs:
            skill_path = home_skills / dirname
            if skill_path.exists():
                backup = _backup(skill_path / "SKILL.md")
                # Don't delete — just rename to .disabled
                disabled = skill_path.with_name(f"{skill_path.name}.disabled")
                skill_path.rename(disabled)
                changes.append({
                    "tool": "claude-code",
                    "action": "disabled_competing_skill",
                    "path": str(disabled),
                    "backup": backup,
                    "detail": f"Disabled competing skill: {dirname} (renamed to .disabled)",
                })
                warnings.append(
                    f"Disabled skill '{dirname}' → '{disabled.name}'. "
                    f"Delete manually if no longer needed."
                )

    return {
        "changes": changes,
        "warnings": warnings,
        "backups_dir": str(get_backups_dir()),
    }
