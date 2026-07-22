"""Agent Eyes readiness, installation, and client configuration."""

from .scanner import scan_ai_tools, scan_competitors
from .state import get_state, mark_initialized, is_first_run
from .configurator import apply_setup
from .readiness import (
    CapabilityProbe,
    ReadinessReport,
    ReadinessStatus,
    ReadinessStore,
    probe_current_readiness,
)

__all__ = [
    "scan_ai_tools",
    "scan_competitors",
    "get_state",
    "mark_initialized",
    "is_first_run",
    "apply_setup",
    "CapabilityProbe",
    "ReadinessReport",
    "ReadinessStatus",
    "ReadinessStore",
    "probe_current_readiness",
]
