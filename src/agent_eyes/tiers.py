"""Tier dispatch infrastructure for agent-eyes.

Manages which connection tier (Extension, CDP, Native) is available
and routes tool calls to the best available tier.
"""
from __future__ import annotations

from enum import IntEnum


class ConnectionTier(IntEnum):
    """Connection tiers, ordered by preference (lower = better)."""
    EXTENSION = 1  # Chrome Extension bridge (best: no flags, cross-platform)
    CDP = 2        # Direct CDP persistent WebSocket (needs --remote-debugging-port)
    NATIVE = 3     # Native AX + AppleScript (always available, limited web)


class TierManager:
    """Tracks which tiers are currently available."""

    def __init__(self) -> None:
        self._available: dict[ConnectionTier, bool] = {
            ConnectionTier.EXTENSION: False,
            ConnectionTier.CDP: False,
            ConnectionTier.NATIVE: True,  # Always available
        }

    def is_available(self, tier: ConnectionTier) -> bool:
        return self._available.get(tier, False)

    def set_available(self, tier: ConnectionTier, available: bool) -> None:
        if tier == ConnectionTier.NATIVE:
            return  # Native is always available
        self._available[tier] = available

    def best_tier(self) -> ConnectionTier:
        """Return the best (lowest value) available tier."""
        for tier in ConnectionTier:
            if self._available.get(tier, False):
                return tier
        return ConnectionTier.NATIVE
