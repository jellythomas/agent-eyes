from __future__ import annotations

import asyncio

import pytest

from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode
from agent_eyes.target_resolver import (
    InventoryCacheStatus,
    ProviderTarget,
    ResolutionSource,
    TargetResolver,
)
from agent_eyes.transaction_contract import TargetMode, TargetSpec


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _browser(
    *,
    pid: int = 41,
    title: str = "Agent Eyes pull request",
    url: str = "https://bitbucket.example/pull-requests/42",
    browser: str = "Firefox",
) -> BrowserTarget:
    return BrowserTarget(
        browser=browser,
        pid=pid,
        title=title,
        url=url,
        window_index=0,
        tab_index=0,
    )


def test_exact_native_target_is_resolved_only_from_an_observed_live_lease():
    async def run() -> None:
        provider = object()
        adapter = object()
        browser = _browser()

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("exact native resolution must not rescan inventory")

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspection must not activate the target")

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[browser],
        )
        resolution = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, target_id=browser.identifier),
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert resolution.target.target_id == browser.identifier
        assert resolution.target.pid == browser.pid
        assert resolution.target.browser_target is browser
        assert resolution.target.source is ResolutionSource.EXACT
        assert resolution.cache_status is InventoryCacheStatus.HIT
        assert resolution.activated is False

    asyncio.run(run())


def test_exact_shadow_target_is_validated_without_foreground_activation():
    async def run() -> None:
        provider = object()
        adapter = object()
        shadow = ProviderTarget(target_id="cdp:session-7")
        activation_calls = 0

        async def inventory(_provider, _adapter, mode):
            assert mode is TargetMode.SHADOW
            return [shadow]

        async def activate(_provider, _adapter, _target):
            nonlocal activation_calls
            activation_calls += 1
            return True

        resolver = TargetResolver(inventory, activate)
        resolution = await resolver.resolve(
            TargetSpec(mode=TargetMode.SHADOW, target_id=shadow.target_id),
            provider_identity=provider,
            adapter_identity=adapter,
            activate=True,
        )

        assert resolution.target.target_id == shadow.target_id
        assert resolution.target.pid is None
        assert resolution.target.source is ResolutionSource.EXACT
        assert resolution.target.provider_target is shadow
        assert resolution.cache_status is InventoryCacheStatus.MISS
        assert resolution.activated is False
        assert activation_calls == 0

    asyncio.run(run())


def test_exact_pid_desktop_target_bypasses_inventory_and_can_activate():
    async def run() -> None:
        provider = object()
        adapter = object()
        activated_target_ids: list[str] = []

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("an exact PID must not scan browser inventory")

        async def activate(_provider, _adapter, target):
            activated_target_ids.append(target.target_id)
            return True

        resolver = TargetResolver(inventory, activate)
        resolution = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
            provider_identity=provider,
            adapter_identity=adapter,
            activate=True,
        )

        assert resolution.target.target_id == "pid:73"
        assert resolution.target.pid == 73
        assert resolution.target.source is ResolutionSource.PID
        assert resolution.cache_status is InventoryCacheStatus.BYPASS
        assert resolution.activated is True
        assert activated_target_ids == ["pid:73"]

    asyncio.run(run())


def test_browser_query_reuses_ranking_and_activates_only_the_winner():
    async def run() -> None:
        unrelated = _browser(pid=1, title="Unrelated news", url="https://news.example")
        winner = _browser(pid=2)
        activated = []

        async def inventory(_provider, _adapter, _mode):
            return [unrelated, winner]

        async def activate(_provider, _adapter, target):
            activated.append(target)
            return True

        resolver = TargetResolver(inventory, activate)
        resolution = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes pull request"),
            provider_identity=object(),
            adapter_identity=object(),
            activate=True,
        )

        assert resolution.target.browser_target is not None
        assert resolution.target.browser_target.pid == winner.pid
        assert resolution.target.browser_target.score > 0
        assert resolution.target.source is ResolutionSource.QUERY
        assert resolution.activated is True
        assert activated == [resolution.target]

    asyncio.run(run())


def test_equal_top_query_scores_fail_closed_as_ambiguous():
    async def run() -> None:
        targets = [
            _browser(pid=1, browser="Firefox"),
            _browser(pid=2, browser="Safari"),
        ]

        async def inventory(_provider, _adapter, _mode):
            return targets

        async def activate(_provider, _adapter, _target):
            raise AssertionError("an ambiguous target must never be activated")

        resolver = TargetResolver(inventory, activate)
        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes pull request"),
                provider_identity=object(),
                adapter_identity=object(),
                activate=True,
            )

        assert exc_info.value.code is OperationErrorCode.AMBIGUOUS_TARGET

    asyncio.run(run())


def test_missing_query_and_exact_targets_use_stable_not_found_error():
    async def run() -> None:
        async def inventory(_provider, _adapter, _mode):
            return [_browser(title="Unrelated", url="https://example.test")]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate)
        for spec in (
            TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes"),
            TargetSpec(mode=TargetMode.FOREGROUND, target_id="native:missing"),
            TargetSpec(mode=TargetMode.SHADOW, target_id="cdp:missing"),
        ):
            with pytest.raises(OperationError) as exc_info:
                await resolver.resolve(
                    spec,
                    provider_identity=object(),
                    adapter_identity=object(),
                )
            assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND

    asyncio.run(run())


@pytest.mark.parametrize(
    "spec",
    [
        TargetSpec(mode=TargetMode.SHADOW, query="pull request"),
        TargetSpec(mode=TargetMode.SHADOW, pid=42),
        TargetSpec(mode=TargetMode.SHADOW, target_id="native:42:w0"),
        TargetSpec(mode=TargetMode.FOREGROUND, target_id="cdp:session"),
    ],
)
def test_mode_and_selector_combinations_fail_before_inventory(spec):
    async def run() -> None:
        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("invalid mode must fail before inventory")

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate)
        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                spec,
                provider_identity=object(),
                adapter_identity=object(),
            )

        assert exc_info.value.code is OperationErrorCode.MODE_MISMATCH

    asyncio.run(run())


def test_multiple_or_missing_selectors_fail_before_inventory():
    async def run() -> None:
        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("invalid target must fail before inventory")

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate)
        for spec in (
            TargetSpec(mode=TargetMode.FOREGROUND),
            TargetSpec(mode=TargetMode.FOREGROUND, query="PR", pid=42),
        ):
            with pytest.raises(OperationError) as exc_info:
                await resolver.resolve(
                    spec,
                    provider_identity=object(),
                    adapter_identity=object(),
                )
            assert exc_info.value.code is OperationErrorCode.TARGET_MISMATCH

    asyncio.run(run())


def test_completed_inventory_cache_uses_injected_monotonic_clock():
    async def run() -> None:
        clock = FakeClock()
        calls = 0
        provider = object()
        adapter = object()
        browser = _browser()

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            return [browser]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate, clock=clock)
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")

        first = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        clock.advance(0.299)
        second = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        clock.advance(0.002)
        third = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert first.cache_status is InventoryCacheStatus.MISS
        assert second.cache_status is InventoryCacheStatus.HIT
        assert third.cache_status is InventoryCacheStatus.MISS
        assert calls == 2

    asyncio.run(run())


def test_inventory_cache_is_separated_by_provider_adapter_identity_and_mode():
    async def run() -> None:
        calls: list[tuple[object, object, TargetMode]] = []
        provider_a = object()
        provider_b = object()
        adapter_a = object()
        adapter_b = object()
        browser = _browser()
        shadow = ProviderTarget(target_id="cdp:one")

        async def inventory(provider, adapter, mode):
            calls.append((provider, adapter, mode))
            return [shadow] if mode is TargetMode.SHADOW else [browser]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate, clock=FakeClock())
        foreground = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")
        shadow_spec = TargetSpec(mode=TargetMode.SHADOW, target_id="cdp:one")

        first = await resolver.resolve(
            foreground,
            provider_identity=provider_a,
            adapter_identity=adapter_a,
        )
        hit = await resolver.resolve(
            foreground,
            provider_identity=provider_a,
            adapter_identity=adapter_a,
        )
        other_adapter = await resolver.resolve(
            foreground,
            provider_identity=provider_a,
            adapter_identity=adapter_b,
        )
        other_provider = await resolver.resolve(
            foreground,
            provider_identity=provider_b,
            adapter_identity=adapter_a,
        )
        other_mode = await resolver.resolve(
            shadow_spec,
            provider_identity=provider_a,
            adapter_identity=adapter_a,
        )

        assert first.cache_status is InventoryCacheStatus.MISS
        assert hit.cache_status is InventoryCacheStatus.HIT
        assert other_adapter.cache_status is InventoryCacheStatus.MISS
        assert other_provider.cache_status is InventoryCacheStatus.MISS
        assert other_mode.cache_status is InventoryCacheStatus.MISS
        assert len(calls) == 4

    asyncio.run(run())


def test_concurrent_inventory_misses_are_single_flight():
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        provider = object()
        adapter = object()

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return [_browser()]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate)
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")
        first_task = asyncio.create_task(
            resolver.resolve(
                spec,
                provider_identity=provider,
                adapter_identity=adapter,
            )
        )
        await started.wait()
        second_task = asyncio.create_task(
            resolver.resolve(
                spec,
                provider_identity=provider,
                adapter_identity=adapter,
            )
        )
        asyncio.get_running_loop().call_soon(release.set)

        first, second = await asyncio.gather(first_task, second_task)

        assert calls == 1
        assert {first.cache_status, second.cache_status} == {
            InventoryCacheStatus.MISS,
            InventoryCacheStatus.SHARED,
        }

    asyncio.run(run())


def test_cached_targets_peeks_only_live_completed_inventory():
    async def run() -> None:
        clock = FakeClock()
        provider = object()
        adapter = object()
        browser = _browser()

        async def inventory(_provider, _adapter, _mode):
            return [browser]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate, clock=clock)
        assert (
            resolver.cached_targets(
                provider_identity=provider,
                adapter_identity=adapter,
                mode=TargetMode.FOREGROUND,
            )
            == ()
        )

        await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes"),
            provider_identity=provider,
            adapter_identity=adapter,
        )
        assert resolver.cached_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
        ) == (browser,)

        clock.advance(1.0)
        assert (
            resolver.cached_targets(
                provider_identity=provider,
                adapter_identity=adapter,
                mode=TargetMode.FOREGROUND,
            )
            == ()
        )

    asyncio.run(run())


def test_failed_and_cancelled_producers_are_not_cached():
    async def run_failure(error) -> None:
        calls = 0
        browser = _browser()
        provider = object()
        adapter = object()

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            return [browser]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate)
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")
        with pytest.raises(type(error)):
            await resolver.resolve(
                spec,
                provider_identity=provider,
                adapter_identity=adapter,
            )

        recovered = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        assert recovered.cache_status is InventoryCacheStatus.MISS
        assert calls == 2

    asyncio.run(run_failure(RuntimeError("provider unavailable")))
    asyncio.run(run_failure(asyncio.CancelledError()))


def test_explicit_invalidation_detaches_inflight_and_completed_entries():
    async def run() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0
        provider = object()
        adapter = object()
        browser = _browser()

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
            return [browser]

        async def activate(_provider, _adapter, _target):
            return True

        resolver = TargetResolver(inventory, activate, clock=FakeClock())
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")
        stale_task = asyncio.create_task(
            resolver.resolve(
                spec,
                provider_identity=provider,
                adapter_identity=adapter,
            )
        )
        await first_started.wait()

        resolver.invalidate(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
        )
        fresh = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        release_first.set()
        stale = await stale_task
        cached = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert stale.cache_status is InventoryCacheStatus.MISS
        assert fresh.cache_status is InventoryCacheStatus.MISS
        assert cached.cache_status is InventoryCacheStatus.HIT
        assert calls == 2

        resolver.invalidate()
        after_global_invalidation = await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        assert after_global_invalidation.cache_status is InventoryCacheStatus.MISS
        assert calls == 3

    asyncio.run(run())


def test_activation_failure_is_stable_and_invalidates_cached_inventory():
    async def run() -> None:
        calls = 0
        provider = object()
        adapter = object()

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            return [_browser()]

        async def activate(_provider, _adapter, _target):
            return False

        resolver = TargetResolver(inventory, activate, clock=FakeClock())
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes")

        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                spec,
                provider_identity=provider,
                adapter_identity=adapter,
                activate=True,
            )
        assert exc_info.value.code is OperationErrorCode.FOCUS_MISMATCH

        await resolver.resolve(
            spec,
            provider_identity=provider,
            adapter_identity=adapter,
        )
        assert calls == 2

    asyncio.run(run())


def test_successful_browser_activation_preserves_and_updates_cached_inventory():
    async def run() -> None:
        calls = 0
        provider = object()
        adapter = object()
        winner = _browser(pid=41)
        previous = _browser(
            pid=73,
            title="Previously selected",
            url="https://example.test/previous",
        )
        previous.selected = True
        previous.frontmost = True

        async def inventory(_provider, _adapter, _mode):
            nonlocal calls
            calls += 1
            return [winner, previous]

        async def activate(_provider, _adapter, target):
            assert target.target_id == winner.identifier
            return True

        clock = FakeClock()
        resolver = TargetResolver(inventory, activate, clock=clock)
        selected = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes"),
            provider_identity=provider,
            adapter_identity=adapter,
            activate=True,
        )
        cached = resolver.cached_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
        )

        assert calls == 1
        assert isinstance(cached[0], BrowserTarget)
        assert cached[0].selected is True
        assert cached[0].frontmost is True
        assert isinstance(cached[1], BrowserTarget)
        assert cached[1].selected is False
        assert cached[1].frontmost is False

        clock.advance(0.5)
        exact = await resolver.resolve(
            TargetSpec(
                mode=TargetMode.FOREGROUND,
                target_id=selected.target.target_id,
            ),
            provider_identity=provider,
            adapter_identity=adapter,
            activate=True,
        )
        assert exact.cache_status is InventoryCacheStatus.HIT
        assert calls == 1

        query_after_base_cache_expiry = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes"),
            provider_identity=provider,
            adapter_identity=adapter,
        )
        assert query_after_base_cache_expiry.cache_status is InventoryCacheStatus.MISS
        assert calls == 2

        resolver.invalidate(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
        )
        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                TargetSpec(
                    mode=TargetMode.FOREGROUND,
                    target_id=selected.target.target_id,
                ),
                provider_identity=provider,
                adapter_identity=adapter,
                activate=True,
            )
        assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND
        assert calls == 2

    asyncio.run(run())


def test_exact_browser_target_never_rebinds_to_same_content_replacement():
    async def run() -> None:
        provider = object()
        adapter = object()
        original = _browser(pid=41)
        replacement = _browser(pid=41)

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("exact native resolution must not rescan inventory")

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspection must not activate")

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[original, replacement],
        )
        resolved = await resolver.resolve(
            TargetSpec(mode=TargetMode.FOREGROUND, target_id=original.identifier),
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert original.identifier != replacement.identifier
        assert resolved.target.target_id == original.identifier
        assert resolved.target.browser_target is original
        assert resolved.target.source is ResolutionSource.EXACT

    asyncio.run(run())


def test_expired_exact_browser_lease_fails_without_inventory_fallback():
    async def run() -> None:
        provider = object()
        adapter = object()
        original = _browser(pid=41)
        inventory_calls = 0

        async def inventory(_provider, _adapter, _mode):
            nonlocal inventory_calls
            inventory_calls += 1
            return [_browser(pid=41)]

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspection must not activate")

        clock = FakeClock()
        resolver = TargetResolver(inventory, activate, clock=clock)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[original],
        )
        clock.advance(61.0)
        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                TargetSpec(mode=TargetMode.FOREGROUND, target_id=original.identifier),
                provider_identity=provider,
                adapter_identity=adapter,
            )

        assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND
        assert inventory_calls == 0

    asyncio.run(run())


def test_repeated_complete_inventories_replace_obsolete_exact_leases() -> None:
    async def inventory(_provider, _adapter, _mode):
        raise AssertionError("lease retention must not trigger inventory")

    async def activate(_provider, _adapter, _target):
        raise AssertionError("lease retention must not activate")

    provider = object()
    adapter = object()
    resolver = TargetResolver(inventory, activate, clock=lambda: 1.0)
    latest = None
    for index in range(10_000):
        latest = _browser(pid=41, title=f"Target {index}")
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[latest],
        )

    bucket = next(iter(resolver._exact_leases.values()))
    assert list(bucket) == [latest.identifier]


def test_resolution_and_activation_are_bounded_by_the_shared_operation_budget():
    async def run() -> None:
        provider = object()
        adapter = object()

        async def inventory(_provider, _adapter, _mode):
            await asyncio.Event().wait()

        async def activate(_provider, _adapter, _target):
            raise AssertionError("activation must not run after inventory deadline")

        resolver = TargetResolver(inventory, activate)
        with pytest.raises(OperationError) as exc_info:
            await resolver.resolve(
                TargetSpec(mode=TargetMode.FOREGROUND, query="agent eyes"),
                provider_identity=provider,
                adapter_identity=adapter,
                budget=OperationBudget.start(0.001),
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED

    asyncio.run(run())
