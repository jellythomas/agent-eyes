"""First-run detection and setup state management."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _state_dir() -> Path:
    """Get the state directory, platform-aware."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agent-eyes"
    elif sys.platform == "win32":
        appdata = Path.home() / "AppData" / "Roaming"
        return appdata / "agent-eyes"
    else:
        return Path.home() / ".config" / "agent-eyes"


def _state_file() -> Path:
    return _state_dir() / "state.json"


def _default_state() -> dict:
    return {
        "initialized": False,
        "initialized_at": None,
        "version": None,
        "tools_configured": [],
        "competitors_replaced": [],
    }


def get_state() -> dict:
    """Read the current setup state."""
    path = _state_file()
    if not path.exists():
        return _default_state()
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_state()


def save_state(state: dict) -> None:
    """Persist setup state."""
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def is_first_run() -> bool:
    """Check if this is the first time agent-eyes is running."""
    return not get_state().get("initialized", False)


def needs_rescan(current_version: str) -> bool:
    """Check if a rescan is needed (version upgrade)."""
    state = get_state()
    if not state.get("initialized"):
        return True
    return state.get("version") != current_version


def mark_initialized(
    version: str,
    tools_configured: list[str],
    competitors_replaced: list[str],
) -> None:
    """Mark setup as complete."""
    state = get_state()
    state.update({
        "initialized": True,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "tools_configured": tools_configured,
        "competitors_replaced": competitors_replaced,
    })
    save_state(state)


def get_backups_dir() -> Path:
    """Get the backups directory for config file backups."""
    d = _state_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d
