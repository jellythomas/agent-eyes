"""agent-eyes setup — Smart scan and onboarding for AI tool integration."""

from .scanner import scan_ai_tools, scan_competitors
from .state import get_state, mark_initialized, is_first_run
from .configurator import apply_setup

__all__ = [
    "scan_ai_tools",
    "scan_competitors",
    "get_state",
    "mark_initialized",
    "is_first_run",
    "apply_setup",
]
