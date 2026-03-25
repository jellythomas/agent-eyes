"""Tests for tier dispatch infrastructure."""
import pytest
from agent_eyes.tiers import ConnectionTier, TierManager


class TestConnectionTier:
    def test_tier_ordering(self):
        assert ConnectionTier.EXTENSION.value < ConnectionTier.CDP.value
        assert ConnectionTier.CDP.value < ConnectionTier.NATIVE.value

    def test_tier_names(self):
        assert ConnectionTier.EXTENSION.name == "EXTENSION"
        assert ConnectionTier.CDP.name == "CDP"
        assert ConnectionTier.NATIVE.name == "NATIVE"


class TestTierManager:
    def test_native_always_available(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.NATIVE) is True

    def test_cdp_unavailable_by_default(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.CDP) is False

    def test_extension_unavailable_by_default(self):
        mgr = TierManager()
        assert mgr.is_available(ConnectionTier.EXTENSION) is False

    def test_best_tier_defaults_to_native(self):
        mgr = TierManager()
        assert mgr.best_tier() == ConnectionTier.NATIVE

    def test_set_cdp_available(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        assert mgr.best_tier() == ConnectionTier.CDP

    def test_set_extension_available_wins(self):
        mgr = TierManager()
        mgr.set_available(ConnectionTier.CDP, True)
        mgr.set_available(ConnectionTier.EXTENSION, True)
        assert mgr.best_tier() == ConnectionTier.EXTENSION
