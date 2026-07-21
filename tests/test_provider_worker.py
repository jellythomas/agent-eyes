from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict

import pytest

from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode
from agent_eyes.provider_worker import ProviderCallState, ProviderWorker


async def _warm(worker: ProviderWorker) -> None:
    await worker.run(
        lambda: None,
        budget=OperationBudget.start(1.0),
        operation="test worker warmup",
    )


def test_sync_provider_work_runs_off_the_event_loop():
    async def run() -> None:
        worker = ProviderWorker("test-provider")
        caller_thread = threading.get_ident()

        result = await worker.run(
            threading.get_ident,
            budget=OperationBudget.start(1.0),
            operation="thread identity",
        )
        await worker.aclose()

        assert result != caller_thread

    asyncio.run(run())


def test_four_provider_lanes_start_lazily_and_remain_single_threaded():
    async def run() -> None:
        marker = f"four-lane-{time.monotonic_ns()}"
        workers = [ProviderWorker(f"{marker}-{index}") for index in range(4)]

        def owned_threads() -> list[threading.Thread]:
            prefix = f"agent-eyes-{marker}-"
            return [
                thread
                for thread in threading.enumerate()
                if thread.name.startswith(prefix)
            ]

        # ThreadPoolExecutor is constructed with each lane, but its sole worker
        # thread must remain lazy until that provider is first used.
        assert owned_threads() == []
        try:
            initial_thread_ids = await asyncio.gather(
                *(
                    worker.run(
                        threading.get_ident,
                        budget=OperationBudget.start(1.0),
                        operation=f"initialize lane {index}",
                    )
                    for index, worker in enumerate(workers)
                )
            )

            assert len(set(initial_thread_ids)) == 4
            assert len(owned_threads()) == 4

            repeated_thread_ids = await asyncio.gather(
                *(
                    workers[index].run(
                        threading.get_ident,
                        budget=OperationBudget.start(1.0),
                        operation=f"reuse lane {index} call {call_index}",
                    )
                    for index in range(4)
                    for call_index in range(8)
                )
            )
            for index, initial_thread_id in enumerate(initial_thread_ids):
                lane_results = repeated_thread_ids[index * 8 : (index + 1) * 8]
                assert lane_results == [initial_thread_id] * 8
            assert len(owned_threads()) == 4
        finally:
            await asyncio.gather(*(worker.aclose() for worker in workers))

        assert owned_threads() == []

    asyncio.run(run())


def test_timeout_returns_promptly_and_quarantines_late_worker():
    async def run() -> None:
        worker = ProviderWorker("test-provider")
        first_started = threading.Event()
        release_first = threading.Event()
        second_ran = threading.Event()
        active = 0
        max_active = 0
        active_guard = threading.Lock()

        def first() -> str:
            nonlocal active, max_active
            with active_guard:
                active += 1
                max_active = max(max_active, active)
            first_started.set()
            release_first.wait(timeout=1.0)
            with active_guard:
                active -= 1
            return "first"

        def second() -> str:
            nonlocal active, max_active
            with active_guard:
                active += 1
                max_active = max(max_active, active)
            second_ran.set()
            with active_guard:
                active -= 1
            return "second"

        await _warm(worker)
        started = time.monotonic()
        with pytest.raises(OperationError) as first_error:
            await worker.run(
                first,
                budget=OperationBudget.start(0.1),
                operation="slow native query",
            )
        elapsed = time.monotonic() - started
        assert first_error.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert elapsed < 0.2
        assert first_started.is_set()
        assert worker.busy is True

        with pytest.raises(OperationError) as second_error:
            await worker.run(
                second,
                budget=OperationBudget.start(0.01),
                operation="conflicting native action",
            )
        assert second_error.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert second_ran.is_set() is False

        release_first.set()
        await worker.wait_until_idle()

        assert await worker.run(
            second,
            budget=OperationBudget.start(1.0),
            operation="recovered native action",
        ) == "second"
        assert max_active == 1
        await worker.aclose()

    asyncio.run(run())


def test_hung_provider_does_not_retain_timed_out_queue_waiters():
    async def run() -> None:
        worker = ProviderWorker("hung-waiter-retention-test")
        started = threading.Event()
        release = threading.Event()
        queued_call_ran = threading.Event()

        def blocking() -> None:
            started.set()
            release.wait()

        active_task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(10.0),
                operation="hung provider",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)

        try:
            results = await asyncio.gather(
                *(
                    worker.run(
                        queued_call_ran.set,
                        budget=OperationBudget.start(0.01),
                        operation=f"timed-out queued call {index}",
                    )
                    for index in range(1_000)
                ),
                return_exceptions=True,
            )

            assert all(
                isinstance(result, OperationError)
                and result.code is OperationErrorCode.DEADLINE_EXCEEDED
                for result in results
            )
            assert queued_call_ran.is_set() is False
            with worker._guard:
                assert len(worker._waiters) == 0
            assert worker.busy is True
        finally:
            active_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active_task
            release.set()
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_cancelled_queue_waiter_is_removed_without_reordering_survivors():
    async def run() -> None:
        worker = ProviderWorker("cancelled-waiter-fairness-test")
        started = threading.Event()
        release = threading.Event()
        execution_order: list[str] = []

        def blocking() -> str:
            started.set()
            release.wait()
            return "active"

        def record(label: str) -> str:
            execution_order.append(label)
            return label

        active_task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(10.0),
                operation="active provider",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)

        first_task = asyncio.create_task(
            worker.run(
                lambda: record("first"),
                budget=OperationBudget.start(5.0),
                operation="first queued call",
            )
        )
        cancelled_task = asyncio.create_task(
            worker.run(
                lambda: record("cancelled"),
                budget=OperationBudget.start(5.0),
                operation="cancelled queued call",
            )
        )
        last_task = asyncio.create_task(
            worker.run(
                lambda: record("last"),
                budget=OperationBudget.start(5.0),
                operation="last queued call",
            )
        )

        tasks = (active_task, first_task, cancelled_task, last_task)
        try:
            async def wait_for_queued_tasks() -> None:
                while True:
                    with worker._guard:
                        if len(worker._waiters) == 3:
                            return
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_queued_tasks(), timeout=1.0)

            cancelled_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_task
            with worker._guard:
                assert len(worker._waiters) == 2

            release.set()
            assert await active_task == "active"
            assert await asyncio.gather(first_task, last_task) == ["first", "last"]
            assert execution_order == ["first", "last"]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_call_state_distinguishes_late_started_timeout_from_queue_timeout():
    async def run() -> None:
        worker = ProviderWorker("dispatch-state-test")
        first_started = threading.Event()
        release_first = threading.Event()

        def blocking() -> None:
            first_started.set()
            release_first.wait(timeout=1.0)

        await _warm(worker)
        started_state = ProviderCallState()
        with pytest.raises(OperationError):
            await worker.run(
                blocking,
                budget=OperationBudget.start(0.1),
                operation="late mutation",
                state=started_state,
            )

        assert first_started.is_set()
        assert started_state.submitted is True
        assert started_state.started is True
        assert started_state.may_have_run is True

        queued_state = ProviderCallState()
        with pytest.raises(OperationError):
            await worker.run(
                lambda: None,
                budget=OperationBudget.start(0.01),
                operation="queued mutation",
                state=queued_state,
            )

        assert queued_state.submitted is False
        assert queued_state.started is False
        assert queued_state.may_have_run is False

        release_first.set()
        await worker.wait_until_idle()
        await worker.aclose()

    asyncio.run(run())


def test_deadline_expiring_after_queue_acquisition_prevents_submission():
    async def run() -> None:
        worker = ProviderWorker("dispatch-checkpoint-test")
        current = 0.0

        def clock() -> float:
            return current

        def expire() -> None:
            nonlocal current
            current = 2.0

        acquire_lane = worker._acquire_lane

        async def acquire_then_expire(*, budget, allow_closed=False):
            await acquire_lane(budget=budget, allow_closed=allow_closed)
            expire()

        worker._acquire_lane = acquire_then_expire
        state = ProviderCallState()
        applied = False

        def mutation() -> None:
            nonlocal applied
            applied = True

        try:
            with pytest.raises(OperationError) as exc_info:
                await worker.run(
                    mutation,
                    budget=OperationBudget.start(1.0, clock=clock),
                    operation="mutation after queue",
                    state=state,
                )

            assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
            assert state.submitted is False
            assert state.started is False
            assert applied is False
            assert worker.busy is False
        finally:
            await worker.aclose()

    asyncio.run(run())


def test_timed_out_lane_recovers_after_originating_event_loop_closes():
    worker = ProviderWorker("cross-loop-recovery-test")
    started = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(timeout=1.0)

    async def time_out_first_call() -> None:
        await _warm(worker)
        with pytest.raises(OperationError):
            await worker.run(
                blocking,
                budget=OperationBudget.start(0.1),
                operation="cross-loop blocking call",
            )

    asyncio.run(time_out_first_call())
    assert started.is_set()
    assert worker.busy is True

    release.set()
    asyncio.run(worker.wait_until_idle())

    async def reuse() -> str:
        return await worker.run(
            lambda: "recovered",
            budget=OperationBudget.start(1.0),
            operation="cross-loop recovered call",
        )

    assert asyncio.run(reuse()) == "recovered"
    asyncio.run(worker.aclose())


def test_abandoned_cross_loop_handoff_is_reclaimed():
    worker = ProviderWorker("abandoned-handoff-test")
    started = threading.Event()
    release = threading.Event()
    handoff_dropped = threading.Event()
    abandoned_task = None
    first_loop = asyncio.new_event_loop()

    async def abandon_handoff():
        nonlocal abandoned_task

        def blocking() -> None:
            started.set()
            release.wait(timeout=1.0)

        active_task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(1.0),
                operation="active cross-loop call",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)

        abandoned_task = asyncio.create_task(
            worker.run(
                lambda: "must not run",
                budget=OperationBudget.start(1.0),
                operation="abandoned cross-loop waiter",
            )
        )
        while True:
            with worker._guard:
                if len(worker._waiters) == 1:
                    break
            await asyncio.sleep(0)

        original_call_soon_threadsafe = first_loop.call_soon_threadsafe

        def drop_provider_handoff(callback, *args, context=None):
            if (
                getattr(callback, "__self__", None) is worker
                and getattr(callback, "__func__", None)
                is ProviderWorker._resolve_waiter
            ):
                handoff_dropped.set()
                return None
            return original_call_soon_threadsafe(
                callback,
                *args,
                context=context,
            )

        first_loop.call_soon_threadsafe = drop_provider_handoff
        release.set()
        await active_task
        while not handoff_dropped.is_set():
            await asyncio.sleep(0)

    first_loop.run_until_complete(abandon_handoff())
    assert abandoned_task is not None
    abandoned_task._log_destroy_pending = False
    recovery_queued = threading.Event()
    recovery_completed = threading.Event()
    recovery_errors: list[BaseException] = []

    class ObservedWaiters(OrderedDict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            recovery_queued.set()

    with worker._guard:
        worker._waiters = ObservedWaiters(worker._waiters)

    def recover() -> None:
        try:
            asyncio.run(worker.wait_until_idle())
        except BaseException as exc:
            recovery_errors.append(exc)
        finally:
            recovery_completed.set()

    recovery_thread = threading.Thread(target=recover, daemon=True)
    recovery_thread.start()
    queued_before_close = recovery_queued.wait(timeout=1.0)
    first_loop.close()
    recovered_without_third_caller = recovery_completed.wait(timeout=0.5)
    try:
        if recovery_thread.is_alive():
            asyncio.run(worker.wait_until_idle())
        recovery_thread.join(timeout=1.0)

        assert queued_before_close is True
        assert recovered_without_third_caller is True
        assert recovery_thread.is_alive() is False
        assert recovery_errors == []

        async def reuse() -> str:
            return await worker.run(
                lambda: "recovered",
                budget=OperationBudget.start(1.0),
                operation="recovered after abandoned handoff",
            )

        assert asyncio.run(reuse()) == "recovered"
    finally:
        asyncio.run(worker.aclose())
        abandoned_task.get_coro().close()


def test_idle_waiter_keeps_fifo_position_during_handoff_liveness_checks():
    async def run() -> None:
        worker = ProviderWorker("idle-waiter-fairness-test")
        started = threading.Event()
        release = threading.Event()
        completion_order: list[str] = []
        liveness_rechecked = asyncio.Event()
        recheck_count = 0

        def blocking() -> str:
            started.set()
            release.wait(timeout=1.0)
            return "active"

        original_reclaim = worker._reclaim_abandoned_handoff

        def observe_reclaim() -> bool:
            nonlocal recheck_count
            recheck_count += 1
            if recheck_count >= 2:
                liveness_rechecked.set()
            return original_reclaim()

        worker._reclaim_abandoned_handoff = observe_reclaim
        active_task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(1.0),
                operation="active provider call",
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)

        async def wait_for_idle() -> None:
            await worker.wait_until_idle()
            completion_order.append("idle")

        idle_task = asyncio.create_task(wait_for_idle())

        async def wait_for_queue_size(size: int) -> None:
            while True:
                with worker._guard:
                    if len(worker._waiters) == size:
                        return
                await asyncio.sleep(0)

        tasks = [active_task, idle_task]
        try:
            await asyncio.wait_for(wait_for_queue_size(1), timeout=1.0)
            with worker._guard:
                idle_future = next(iter(worker._waiters))

            trailing_task = asyncio.create_task(
                worker.run(
                    lambda: completion_order.append("trailing"),
                    budget=OperationBudget.start(1.0),
                    operation="trailing provider call",
                )
            )
            tasks.append(trailing_task)
            await asyncio.wait_for(wait_for_queue_size(2), timeout=1.0)
            await asyncio.wait_for(liveness_rechecked.wait(), timeout=1.0)

            with worker._guard:
                queued_futures = list(worker._waiters)
            assert queued_futures[0] is idle_future

            release.set()
            assert await active_task == "active"
            await asyncio.gather(idle_task, trailing_task)
            assert completion_order == ["idle", "trailing"]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_late_provider_exception_is_consumed_without_loop_secret_leak():
    secret = "late-provider-sentinel-secret"

    async def run() -> None:
        worker = ProviderWorker("late-exception-test")
        release = threading.Event()
        captured: list[dict] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: captured.append(context))

        def fail_late() -> None:
            release.wait(timeout=1.0)
            raise RuntimeError(secret)

        await _warm(worker)
        with pytest.raises(OperationError):
            await worker.run(
                fail_late,
                budget=OperationBudget.start(0.1),
                operation="late failing provider",
            )
        release.set()
        await worker.wait_until_idle()
        await asyncio.sleep(0)
        await worker.aclose()

        assert secret not in repr(captured)
        assert captured == []

    asyncio.run(run())


def test_cancelled_close_can_be_retried_from_a_new_event_loop():
    marker = f"cancel-close-{time.monotonic_ns()}"
    worker = ProviderWorker(marker)
    started = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(timeout=1.0)

    async def cancel_first_close() -> None:
        await _warm(worker)
        with pytest.raises(OperationError):
            await worker.run(
                blocking,
                budget=OperationBudget.start(0.1),
                operation="close quarantine",
            )
        close_task = asyncio.create_task(worker.aclose())
        await asyncio.sleep(0)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

    asyncio.run(cancel_first_close())
    assert started.is_set()
    release.set()
    asyncio.run(worker.aclose())

    assert not any(
        thread.name.startswith(f"agent-eyes-{marker}")
        for thread in threading.enumerate()
    )


def test_cancelled_caller_does_not_release_lane_while_sync_work_is_running():
    async def run() -> None:
        worker = ProviderWorker("test-provider")
        started = threading.Event()
        release = threading.Event()

        def blocking() -> None:
            started.set()
            release.wait(timeout=1.0)

        task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(10.0),
                operation="cancelled native query",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert worker.busy is True
        release.set()
        await worker.wait_until_idle()
        assert worker.busy is False
        await worker.aclose()

    asyncio.run(run())


def test_sync_exception_releases_lane_for_next_operation():
    async def run() -> None:
        worker = ProviderWorker("test-provider")

        def fail() -> None:
            raise ValueError("provider failed")

        with pytest.raises(ValueError, match="provider failed"):
            await worker.run(
                fail,
                budget=OperationBudget.start(1.0),
                operation="failing provider",
            )

        assert await worker.run(
            lambda: "recovered",
            budget=OperationBudget.start(1.0),
            operation="recovered provider",
        ) == "recovered"
        await worker.aclose()

    asyncio.run(run())


def test_close_waits_for_active_work_and_rejects_new_work():
    async def run() -> None:
        worker = ProviderWorker("test-provider")
        started = threading.Event()
        release = threading.Event()

        def blocking() -> None:
            started.set()
            release.wait(timeout=1.0)

        task = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(1.0),
                operation="active provider",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        close_task = asyncio.create_task(worker.aclose())
        await asyncio.sleep(0)
        assert close_task.done() is False

        release.set()
        await task
        await close_task

        with pytest.raises(RuntimeError, match="closed"):
            await worker.run(
                lambda: None,
                budget=OperationBudget.start(1.0),
                operation="closed provider",
            )

    asyncio.run(run())


def test_permanently_hung_provider_cannot_block_shutdown_or_process_exit():
    async def run() -> None:
        marker = f"hung-shutdown-{time.monotonic_ns()}"
        worker = ProviderWorker(marker, shutdown_timeout=0.01)
        started = threading.Event()
        release = threading.Event()

        def blocking() -> None:
            started.set()
            release.wait()

        await _warm(worker)
        provider_call = asyncio.create_task(
            worker.run(
                blocking,
                budget=OperationBudget.start(0.1),
                operation="permanently hung provider",
            )
        )
        with pytest.raises(OperationError):
            await provider_call
        assert started.is_set()

        before = time.monotonic()
        await worker.aclose()
        elapsed = time.monotonic() - before

        owned_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name == f"agent-eyes-{marker}"
        ]
        assert elapsed < 0.1
        assert len(owned_threads) == 1
        assert owned_threads[0].daemon is True

        release.set()
        owned_threads[0].join(timeout=1.0)
        assert owned_threads[0].is_alive() is False

    asyncio.run(run())


@pytest.mark.parametrize(
    "shutdown_timeout",
    [-1.0, float("inf"), float("nan"), True, "0.1"],
)
def test_invalid_worker_shutdown_timeout_is_rejected(shutdown_timeout):
    with pytest.raises(ValueError):
        ProviderWorker("invalid-timeout", shutdown_timeout=shutdown_timeout)
