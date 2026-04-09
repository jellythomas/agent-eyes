"""Tests for tier dispatch infrastructure."""
import pytest
from agent_eyes.tiers import ConnectionTier, TierManager


class TestConnectionTier:
    def test_tier_ordering(self):
        assert ConnectionTier.CDP.value < ConnectionTier.NATIVE.value

    def test_tier_names(self):
        assert ConnectionTier.CDP.name == "CDP"
        assert ConnectionTier.NATIVE.name == "NATIVE"

    def test_no_extension_tier(self):
        assert not hasattr(ConnectionTier, "EXTENSION")


class TestTierManager:
    def test_native_always_available(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.NATIVE) is True

    def test_cdp_unavailable_by_default(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.CDP) is False

    def test_best_tier_defaults_to_native(self):
        mgr = TierManager()
        assert mgr.best_tier() == ConnectionTier.NATIVE

    def test_set_cdp_available(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        assert mgr.best_tier() == ConnectionTier.CDP

    def test_cdp_wins_over_native(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        assert mgr.best_tier() == ConnectionTier.CDP
