from __future__ import annotations

import asyncio

import pytest

from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode


def test_foreground_mutations_never_overlap():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        first_started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()
        active = 0
        max_active = 0
        order: list[str] = []

        async def first() -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append("first-start")
            first_started.set()
            await allow_first_to_finish.wait()
            order.append("first-end")
            active -= 1
            return "first"

        async def second() -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append("second-start")
            active -= 1
            return "second"

        first_task = asyncio.create_task(coordinator.execute_foreground(first))
        await first_started.wait()
        second_task = asyncio.create_task(coordinator.execute_foreground(second))
        await asyncio.sleep(0)

        assert order == ["first-start"]

        allow_first_to_finish.set()
        assert await first_task == "first"
        assert await second_task == "second"
        assert max_active == 1
        assert order == ["first-start", "first-end", "second-start"]

    asyncio.run(run())


def test_shadow_mutations_serialize_per_target_but_not_globally():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        target_a_started = asyncio.Event()
        target_b_started = asyncio.Event()
        release = asyncio.Event()
        same_target_started = False

        async def target_a() -> str:
            target_a_started.set()
            await release.wait()
            return "a"

        async def target_a_second() -> str:
            nonlocal same_target_started
            same_target_started = True
            return "a2"

        async def target_b() -> str:
            target_b_started.set()
            await release.wait()
            return "b"

        first = asyncio.create_task(coordinator.execute_shadow("target-a", target_a))
        await target_a_started.wait()
        second = asyncio.create_task(
            coordinator.execute_shadow("target-a", target_a_second)
        )
        other = asyncio.create_task(coordinator.execute_shadow("target-b", target_b))
        await target_b_started.wait()
        await asyncio.sleep(0)

        assert same_target_started is False

        release.set()
        assert await first == "a"
        assert await other == "b"
        assert await second == "a2"

    asyncio.run(run())


def test_identical_observations_are_single_flight():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        producer_started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def producer() -> object:
            nonlocal calls
            calls += 1
            producer_started.set()
            await release.wait()
            return object()

        tasks = [
            asyncio.create_task(coordinator.observe(("native", 42, 6), producer))
            for _ in range(32)
        ]
        await producer_started.wait()
        await asyncio.sleep(0)
        assert calls == 1

        release.set()
        results = await asyncio.gather(*tasks)

        assert calls == 1
        assert all(result is results[0] for result in results)

    asyncio.run(run())


def test_different_observation_keys_never_coalesce():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def first_producer() -> str:
            calls.append("first")
            first_started.set()
            await release.wait()
            return "first-result"

        async def second_producer() -> str:
            calls.append("second")
            second_started.set()
            await release.wait()
            return "second-result"

        first = asyncio.create_task(coordinator.observe("first-key", first_producer))
        second = asyncio.create_task(
            coordinator.observe("second-key", second_producer)
        )
        await asyncio.gather(first_started.wait(), second_started.wait())

        assert sorted(calls) == ["first", "second"]

        release.set()
        assert await asyncio.gather(first, second) == [
            "first-result",
            "second-result",
        ]
        assert coordinator._flights == {}

    asyncio.run(run())


def test_failed_observation_is_removed_so_a_later_call_can_retry():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        calls = 0

        async def producer() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("first failure")
            return "recovered"

        with pytest.raises(ValueError, match="first failure"):
            await coordinator.observe("same-key", producer)

        assert coordinator._flights == {}
        assert await coordinator.observe("same-key", producer) == "recovered"
        assert calls == 2
        assert coordinator._flights == {}

    asyncio.run(run())


def test_cancelling_one_observer_does_not_cancel_shared_provider_work():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def producer() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "done"

        cancelled = asyncio.create_task(coordinator.observe("shared", producer))
        survivor = asyncio.create_task(coordinator.observe("shared", producer))
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        release.set()
        assert await survivor == "done"
        assert calls == 1
        assert coordinator._flights == {}

    asyncio.run(run())


def test_cancelled_only_observer_cancels_discarded_provider_and_cleans_immediately():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def producer() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
                return "done"
            finally:
                cancelled.set()

        observer = asyncio.create_task(coordinator.observe("shared", producer))
        await started.wait()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer

        await cancelled.wait()
        await asyncio.sleep(0)

        assert coordinator._flights == {}

    asyncio.run(run())


def test_foreground_poison_rejects_work_until_recovery_finishes():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        recovery = asyncio.Event()
        ran = False

        coordinator.poison_foreground_until(recovery.wait())

        async def mutation() -> None:
            nonlocal ran
            ran = True

        with pytest.raises(OperationError) as exc_info:
            await coordinator.execute_foreground(mutation)
        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert ran is False

        recovery.set()
        while coordinator._foreground_poison:
            await asyncio.sleep(0)

        await coordinator.execute_foreground(mutation)
        assert ran is True

    asyncio.run(run())


def test_lock_wait_consumes_the_callers_deadline_without_running_action():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        second_ran = False

        async def first() -> None:
            started.set()
            await release.wait()

        async def second() -> None:
            nonlocal second_ran
            second_ran = True

        first_task = asyncio.create_task(coordinator.execute_foreground(first))
        await started.wait()

        with pytest.raises(OperationError) as exc_info:
            await coordinator.execute_foreground(
                second,
                budget=OperationBudget.start(0.01),
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert second_ran is False
        release.set()
        await first_task

    asyncio.run(run())


def test_dispatch_aware_operation_can_own_deadline_without_outer_cancellation():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        budget = OperationBudget.start(0.02)
        dispatched = False

        async def operation() -> str:
            nonlocal dispatched
            dispatched = True
            try:
                await budget.wait_for(
                    asyncio.Event().wait(),
                    operation="dispatched mutation",
                )
            except OperationError as exc:
                assert exc.code is OperationErrorCode.DEADLINE_EXCEEDED
                return "OUTCOME_UNKNOWN"
            raise AssertionError("mutation wait unexpectedly completed")

        result = await coordinator.execute_foreground(
            operation,
            budget=budget,
            operation_manages_deadline=True,
        )

        assert dispatched is True
        assert result == "OUTCOME_UNKNOWN"

    asyncio.run(run())


def test_close_rejects_new_work_and_finishes_single_flight_tasks():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        await coordinator.close()

        async def operation() -> str:
            return "not run"

        with pytest.raises(RuntimeError, match="closed"):
            await coordinator.execute_foreground(operation)
        with pytest.raises(RuntimeError, match="closed"):
            await coordinator.observe("key", operation)

    asyncio.run(run())


def test_internal_single_flight_and_shadow_lock_maps_do_not_leak():
    async def run() -> None:
        coordinator = AutomationCoordinator()

        async def operation() -> str:
            return "done"

        for index in range(10_000):
            assert await coordinator.observe(("key", index), operation) == "done"
            assert await coordinator.execute_shadow(("target", index), operation) == "done"

        assert coordinator._flights == {}
        assert coordinator._shadow_locks == {}

    asyncio.run(run())


def test_cancelled_shadow_waiters_reclaim_their_keyed_lock_entries_under_stress():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> str:
            started.set()
            await release.wait()
            return "done"

        async def queued() -> str:
            return "should not run"

        active = asyncio.create_task(coordinator.execute_shadow("shared", holder))
        await started.wait()
        waiters = [
            asyncio.create_task(coordinator.execute_shadow("shared", queued))
            for _ in range(2_000)
        ]
        await asyncio.sleep(0)
        for waiter in waiters:
            waiter.cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)

        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert coordinator._shadow_locks["shared"].users == 1

        release.set()
        assert await active == "done"
        assert coordinator._shadow_locks == {}

    asyncio.run(run())


def test_close_cancels_and_drains_cooperative_active_producers():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def producer() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        observer = asyncio.create_task(coordinator.observe("active", producer))
        await started.wait()

        await coordinator.close()

        await cancelled.wait()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert coordinator._flights == {}

    asyncio.run(run())


def test_close_has_a_bounded_grace_period_for_a_stubborn_active_producer():
    async def run() -> None:
        coordinator = AutomationCoordinator(shutdown_timeout=0.01)
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def producer() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await finish_cleanup.wait()
                raise

        observer = asyncio.create_task(coordinator.observe("active", producer))
        await started.wait()

        loop = asyncio.get_running_loop()
        before = loop.time()
        await coordinator.close()
        elapsed = loop.time() - before

        assert cancellation_seen.is_set()
        assert elapsed < 0.1
        assert coordinator._flights == {}

        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await observer

    asyncio.run(run())


def test_concurrent_close_callers_wait_for_the_same_shutdown():
    async def run() -> None:
        coordinator = AutomationCoordinator()
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()

        async def producer() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancellation_seen.set()

        observer = asyncio.create_task(coordinator.observe("active", producer))
        await started.wait()
        closes = [asyncio.create_task(coordinator.close()) for _ in range(16)]

        await asyncio.gather(*closes)

        assert cancellation_seen.is_set()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert coordinator._flights == {}

    asyncio.run(run())


@pytest.mark.parametrize(
    "shutdown_timeout",
    [-1.0, float("inf"), float("nan"), True, "0.1"],
)
def test_invalid_shutdown_timeout_is_rejected(shutdown_timeout):
    with pytest.raises(ValueError):
        AutomationCoordinator(shutdown_timeout=shutdown_timeout)
