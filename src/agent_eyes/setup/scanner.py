"""Scan for installed AI tools and competing MCP servers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .competitors import COMPETITORS, CATEGORIES

# ── AI Tool Definitions ─────────────────────────────────────────────

def _home() -> Path:
    return Path.home()


def _ai_tool_definitions() -> list[dict]:
    """Return definitions for all known AI tools with their config paths.

    Each definition includes platform-specific paths and the structure used
    to store MCP server configs.
    """
    home = _home()

    # Platform-specific base paths
    if sys.platform == "darwin":
        code_user = home / "Library" / "Application Support" / "Code" / "User"
        claude_desktop = home / "Library" / "Application Support" / "Claude"
        windsurf_base = home / ".codeium" / "windsurf"
        zed_config = home / ".config" / "zed"
    elif sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        code_user = appdata / "Code" / "User"
        claude_desktop = appdata / "Claude"
        windsurf_base = home / ".codeium" / "windsurf"
        zed_config = appdata / "Zed"
    else:  # Linux
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        code_user = config_home / "Code" / "User"
        claude_desktop = config_home / "Claude"
        windsurf_base = home / ".codeium" / "windsurf"
        zed_config = config_home / "zed"

    return [
        {
            "id": "claude-code",
            "name": "Claude Code",
            "detection_paths": [home / ".claude"],
            "config_locations": {
                "global_mcp": {
                    "path": home / ".claude.json",
                    "key": "mcpServers",
                    "format": "json",
                },
                "project_mcp": {
                    "path": ".mcp.json",  # relative — checked per-project
                    "key": "mcpServers",
                    "format": "json",
                },
                "skills": {
                    "path": home / ".claude" / "skills",
                    "type": "directory",
                },
                "agents": {
                    "path": home / ".claude" / "agents",
                    "type": "directory",
                },
            },
            "supports_skills": True,
            "supports_agents": True,
        },
        {
            "id": "claude-desktop",
            "name": "Claude Desktop",
            "detection_paths": [claude_desktop],
            "config_locations": {
                "global_mcp": {
                    "path": claude_desktop / "claude_desktop_config.json",
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "codex",
            "name": "Codex",
            "detection_paths": [home / ".codex"],
            "config_locations": {
                "global_mcp": {
                    "path": home / ".codex" / "config.toml",
                    "key": "mcp_servers",
                    "format": "toml",
                },
                "project_mcp": {
                    "path": Path(".codex") / "config.toml",
                    "key": "mcp_servers",
                    "format": "toml",
                },
                "skills": {
                    "path": home / ".codex" / "skills",
                    "type": "directory",
                },
            },
            "supports_skills": True,
            "supports_agents": False,
        },
        {
            "id": "cursor",
            "name": "Cursor",
            "detection_paths": [home / ".cursor"],
            "config_locations": {
                "global_mcp": {
                    "path": home / ".cursor" / "mcp.json",
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "vscode",
            "name": "VS Code (Copilot)",
            "detection_paths": [code_user],
            "config_locations": {
                "global_mcp": {
                    "path": code_user / "mcp.json",
                    "key": "servers",  # VS Code uses "servers" not "mcpServers"
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "cline",
            "name": "Cline (VS Code Extension)",
            "detection_paths": [
                code_user / "globalStorage" / "saoudrizwan.claude-dev",
            ],
            "config_locations": {
                "global_mcp": {
                    "path": (
                        code_user / "globalStorage" / "saoudrizwan.claude-dev"
                        / "settings" / "cline_mcp_settings.json"
                    ),
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "roo-code",
            "name": "Roo Code (VS Code Extension)",
            "detection_paths": [
                code_user / "globalStorage" / "rooveterinaryinc.roo-cline",
            ],
            "config_locations": {
                "global_mcp": {
                    "path": (
                        code_user / "globalStorage" / "rooveterinaryinc.roo-cline"
                        / "settings" / "cline_mcp_settings.json"
                    ),
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "windsurf",
            "name": "Windsurf",
            "detection_paths": [windsurf_base],
            "config_locations": {
                "global_mcp": {
                    "path": windsurf_base / "mcp_config.json",
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "zed",
            "name": "Zed",
            "detection_paths": [zed_config],
            "config_locations": {
                "global_mcp": {
                    "path": zed_config / "settings.json",
                    "key": "context_servers",  # Zed uses "context_servers"
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
        {
            "id": "continue",
            "name": "Continue",
            "detection_paths": [home / ".continue"],
            "config_locations": {
                "global_mcp": {
                    "path": home / ".continue" / "config.json",
                    "key": "mcpServers",
                    "format": "json",
                },
            },
            "supports_skills": False,
            "supports_agents": False,
        },
    ]


# ── Scanning ─────────────────────────────────────────────────────────

def scan_ai_tools() -> list[dict]:
    """Detect which AI tools are installed on this machine.

    Returns a list of installed AI tools with their config paths.
    """
    installed = []
    for defn in _ai_tool_definitions():
        detected = False
        for p in defn["detection_paths"]:
            if p.exists():
                detected = True
                break
        if detected:
            installed.append({
                "id": defn["id"],
                "name": defn["name"],
                "config_locations": defn["config_locations"],
                "supports_skills": defn.get("supports_skills", False),
                "supports_agents": defn.get("supports_agents", False),
            })
    return installed


def _match_competitor(server_key: str, server_config: dict, competitor: dict) -> bool:
    """Check if a server config entry matches a known competitor."""
    det = competitor["detection"]

    # Match by server name key (case-insensitive substring)
    key_lower = server_key.lower()
    for pattern in det["keys"]:
        if pattern.lower() in key_lower:
            return True

    # Match by command/args content
    command = str(server_config.get("command", "")).lower()
    args = server_config.get("args", [])
    args_str = " ".join(str(a).lower() for a in args)
    full_cmd = f"{command} {args_str}"

    for pattern in det["args"]:
        if pattern.lower() in full_cmd:
            return True

    return False


def _scan_mcp_config(config_path: Path, key_name: str) -> list[dict]:
    """Scan a single MCP config file for competitor servers.

    Returns list of {competitor_id, competitor_name, server_key, config_path}.
    """
    if not config_path.exists():
        return []

    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    servers = data.get(key_name, {})
    if not isinstance(servers, dict):
        return []

    found = []
    for server_key, server_config in servers.items():
        if not isinstance(server_config, dict):
            continue

        # Skip if this IS agent-eyes
        if "agent-eyes" in server_key.lower() or "agent_eyes" in server_key.lower():
            continue

        for competitor in COMPETITORS:
            if _match_competitor(server_key, server_config, competitor):
                found.append({
                    "competitor_id": competitor["id"],
                    "competitor_name": competitor["name"],
                    "category": competitor["category"],
                    "server_key": server_key,
                    "config_path": str(config_path),
                    "tools_prefix": competitor["tools_prefix"],
                    "tools_count": competitor["tools_count"],
                    "overlap": competitor["overlap"],
                })
                break  # One match per server entry

    return found


def _scan_claude_code_skills(skills_dir: Path) -> list[dict]:
    """Scan Claude Code skills for competing browser/UI skills."""
    found = []
    if not skills_dir.exists():
        return found

    # Known competing skill names
    competing_skills = [
        "playwright-skill", "playwright", "browser-automation",
        "puppeteer-skill", "puppeteer",
    ]

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        dirname = skill_dir.name.lower()
        for cs in competing_skills:
            if cs in dirname:
                found.append({
                    "competitor_id": f"skill:{skill_dir.name}",
                    "competitor_name": f"Skill: {skill_dir.name}",
                    "category": "browser",
                    "location": "skills",
                    "path": str(skill_dir),
                })
                break

    return found


def _scan_claude_code_agents(agents_dir: Path) -> list[dict]:
    """Scan Claude Code agent definitions for competitor tool references.

    Returns list of agents that reference competing MCP tools.
    """
    found = []
    if not agents_dir.exists():
        return found

    # Patterns that indicate competitor tool references in agent tools: lists
    competitor_tool_patterns = [
        (r"mcp__playwright__", "playwright-mcp"),
        (r"mcp__puppeteer__", "puppeteer-mcp"),
        (r"mcp__browserbase__", "browserbase-mcp"),
        (r"mcp__browser-use__", "browser-use-mcp"),
        (r"mcp__selenium__", "selenium-mcp"),
        (r"mcp__desktop-commander__", "desktop-commander"),
        (r"mcp__computer-use__", "computer-use-mcp"),
        (r"mcp__computer-control__", "computer-control-mcp"),
        (r"mcp__peekaboo__", "peekaboo"),
        (r"mcp__screenpipe__", "screenpipe-mcp"),
    ]

    for agent_file in agents_dir.glob("*.md"):
        try:
            content = agent_file.read_text()
        except OSError:
            continue

        for pattern, competitor_id in competitor_tool_patterns:
            if re.search(pattern, content):
                # Count how many tool references
                count = len(re.findall(pattern, content))
                found.append({
                    "competitor_id": competitor_id,
                    "agent_file": agent_file.name,
                    "agent_path": str(agent_file),
                    "tool_refs_count": count,
                })

    return found


def _check_agent_eyes_installed(tool: dict) -> dict:
    """Check if agent-eyes is already configured in an AI tool."""
    result = {"mcp_configured": False, "skill_installed": False}

    # Check MCP configs
    for loc_name, loc in tool["config_locations"].items():
        if loc.get("type") == "directory":
            continue
        path = Path(loc["path"])
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            servers = data.get(loc["key"], {})
            if isinstance(servers, dict):
                for key in servers:
                    if "agent-eyes" in key.lower() or "agent_eyes" in key.lower():
                        result["mcp_configured"] = True
                        break
        except (json.JSONDecodeError, OSError):
            pass

    # Check skills (Claude Code only)
    if tool.get("supports_skills"):
        skills_loc = tool["config_locations"].get("skills", {})
        if skills_loc.get("type") == "directory":
            skills_dir = Path(skills_loc["path"])
            agent_eyes_skill = skills_dir / "agent-eyes"
            if agent_eyes_skill.exists() and (agent_eyes_skill / "SKILL.md").exists():
                result["skill_installed"] = True

    return result


def scan_competitors(ai_tools: list[dict]) -> dict:
    """Scan all installed AI tools for competing MCP servers.

    Returns a structured report:
    {
        "by_tool": {
            "claude-code": {
                "mcp_competitors": [...],
                "skill_competitors": [...],
                "agent_refs": [...],
                "agent_eyes_status": {...}
            },
            ...
        },
        "by_competitor": {
            "playwright-mcp": {
                "found_in": ["claude-code", "cursor"],
                ...
            }
        },
        "by_category": {
            "browser": [...],
            "desktop": [...],
            "vision": [...]
        },
        "summary": {
            "total_competitors": N,
            "total_locations": N,
        }
    }
    """
    by_tool = {}
    by_competitor: dict[str, dict] = {}
    by_category: dict[str, list] = {cat: [] for cat in CATEGORIES}

    for tool in ai_tools:
        tool_report: dict = {
            "mcp_competitors": [],
            "skill_competitors": [],
            "agent_refs": [],
            "agent_eyes_status": _check_agent_eyes_installed(tool),
        }

        # Scan MCP config files
        for loc_name, loc in tool["config_locations"].items():
            if loc.get("type") == "directory":
                continue
            path = Path(loc["path"])
            if str(path).startswith("."):
                continue  # Skip relative project paths
            competitors = _scan_mcp_config(path, loc["key"])
            tool_report["mcp_competitors"].extend(competitors)

        # Scan Claude Code skills
        if tool.get("supports_skills"):
            skills_loc = tool["config_locations"].get("skills", {})
            if skills_loc.get("type") == "directory":
                skills = _scan_claude_code_skills(Path(skills_loc["path"]))
                tool_report["skill_competitors"] = skills

        # Scan Claude Code agents
        if tool.get("supports_agents"):
            agents_loc = tool["config_locations"].get("agents", {})
            if agents_loc.get("type") == "directory":
                refs = _scan_claude_code_agents(Path(agents_loc["path"]))
                tool_report["agent_refs"] = refs

        by_tool[tool["id"]] = tool_report

        # Aggregate by competitor
        for comp in tool_report["mcp_competitors"]:
            cid = comp["competitor_id"]
            if cid not in by_competitor:
                by_competitor[cid] = {
                    "name": comp["competitor_name"],
                    "category": comp["category"],
                    "found_in": [],
                }
            by_competitor[cid]["found_in"].append({
                "tool_id": tool["id"],
                "tool_name": tool["name"],
                "server_key": comp["server_key"],
                "config_path": comp["config_path"],
            })

        # Aggregate by category
        for comp in tool_report["mcp_competitors"]:
            cat = comp["category"]
            if comp["competitor_id"] not in [c["id"] for c in by_category[cat]]:
                by_category[cat].append({
                    "id": comp["competitor_id"],
                    "name": comp["competitor_name"],
                })

    # Summary
    total_competitors = len(by_competitor)
    total_locations = sum(
        len(v["found_in"]) for v in by_competitor.values()
    )

    return {
        "by_tool": by_tool,
        "by_competitor": by_competitor,
        "by_category": by_category,
        "summary": {
            "total_competitors": total_competitors,
            "total_locations": total_locations,
        },
    }
