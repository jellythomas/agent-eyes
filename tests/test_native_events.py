from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.native_events import (
    LinuxATSPIChangeSubscription,
    MacOSAXChangeSubscription,
    NativeChangeSubscription,
    WindowsUIAChangeSubscription,
    create_native_change_subscription,
    open_native_change_subscription,
    run_native_action_until,
    run_native_action_until_any,
    wait_for_native_element,
)
from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode
from agent_eyes.provider_worker import ProviderCallState, ProviderWorker


class FakeAdapter:
    def __init__(self, responses: list[list[UIElement]], on_find=None):
        self.responses = responses
        self.on_find = on_find
        self.calls = 0

    def find_elements(self, pid: int, role: str = "", name: str = "", value: str = ""):
        self.calls += 1
        if self.on_find is not None:
            self.on_find(self.calls)
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]


class FakeSubscription:
    def __init__(self, *, generation: int = 0, changes: list[bool] | None = None):
        self.generation = generation
        self.changes = list(changes or [])
        self.waited_after: list[int] = []
        self.closed = False
        self.active = True

    async def wait_for_change(self, after_generation: int, timeout: float) -> bool:
        self.waited_after.append(after_generation)
        if self.generation > after_generation:
            return True
        if self.changes:
            changed = self.changes.pop(0)
            if changed:
                self.generation += 1
            return changed
        await asyncio.sleep(timeout)
        return False

    async def aclose(self) -> None:
        self.closed = True
        self.active = False


def test_native_action_subscribes_before_action_and_rechecks_on_event():
    async def run():
        subscription = FakeSubscription(changes=[True])
        observations = iter([False, True])
        action_calls = 0

        async def factory(pid: int):
            assert pid == 42
            return subscription

        def action():
            nonlocal action_calls
            action_calls += 1
            return "activated"

        result = await run_native_action_until(
            42,
            action,
            lambda: next(observations),
            timeout=1,
            subscription_factory=factory,
        )

        assert result.action_result == "activated"
        assert result.condition_met is True
        assert result.event_driven is True
        assert result.checks == 2
        assert action_calls == 1
        assert subscription.waited_after == [0]
        assert subscription.closed is True

    asyncio.run(run())


def test_native_action_any_subscribes_to_every_candidate_before_one_dispatch():
    async def run():
        subscriptions = {
            41: FakeSubscription(changes=[False]),
            73: FakeSubscription(changes=[True]),
        }
        observations = iter([False, True])
        opened: list[int] = []
        dispatches = 0

        async def factory(pid: int):
            opened.append(pid)
            return subscriptions[pid]

        def action():
            nonlocal dispatches
            assert opened == [41, 73]
            dispatches += 1
            return "opened"

        result = await run_native_action_until_any(
            (41, 73, 41),
            action,
            lambda: next(observations),
            timeout=1,
            subscription_factory=factory,
        )

        assert result.action_result == "opened"
        assert result.condition_met is True
        assert result.event_driven is True
        assert dispatches == 1
        assert all(subscription.closed for subscription in subscriptions.values())

    asyncio.run(run())


def test_native_action_returns_immediately_when_completion_is_synchronous():
    async def run():
        subscription = FakeSubscription()

        async def factory(pid: int):
            return subscription

        result = await run_native_action_until(
            7,
            lambda: True,
            lambda: True,
            timeout=1,
            subscription_factory=factory,
        )

        assert result.condition_met is True
        assert result.event_driven is True
        assert result.checks == 1
        assert subscription.waited_after == []
        assert subscription.closed is True

    asyncio.run(run())


def test_native_action_can_skip_dispatch_when_condition_is_already_met():
    async def run():
        subscription = FakeSubscription()
        action_calls = 0

        async def factory(pid: int):
            assert pid == 7
            return subscription

        def action():
            nonlocal action_calls
            action_calls += 1
            return "must not dispatch"

        result = await run_native_action_until(
            7,
            action,
            lambda: True,
            timeout=1,
            subscription_factory=factory,
            skip_action_if_condition=True,
        )

        assert result.action_result is None
        assert result.action_dispatched is False
        assert result.condition_met is True
        assert result.checks == 1
        assert action_calls == 0
        assert subscription.closed is True

    asyncio.run(run())


def test_required_multi_process_coverage_rejects_partial_observers_before_dispatch():
    async def run():
        subscription = FakeSubscription()
        action_calls = 0

        async def factory(pid: int):
            return subscription if pid == 41 else None

        def action():
            nonlocal action_calls
            action_calls += 1

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until_any(
                (41, 73),
                action,
                lambda: False,
                timeout=1,
                subscription_factory=factory,
                require_all_subscriptions=True,
            )

        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0
        assert subscription.closed is True

    asyncio.run(run())


def test_preflight_generation_change_prevents_dispatch_and_preserves_retry_safety():
    async def run():
        subscription = FakeSubscription()
        state = ProviderCallState()
        action_calls = 0

        def condition() -> bool:
            subscription.generation += 1
            return False

        def action():
            nonlocal action_calls
            action_calls += 1

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                73,
                action,
                condition,
                timeout=1,
                subscription_factory=lambda _pid: subscription,
                action_state=state,
                skip_action_if_condition=True,
                require_subscription_for_dispatch=True,
                abort_dispatch_on_change=True,
            )

        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0
        assert state.started is False
        assert state.may_have_run is False

    asyncio.run(run())


def test_worker_side_guard_rechecks_generation_after_waiting_for_action_lane():
    async def run():
        subscription = FakeSubscription()
        worker = ProviderWorker("native-predispatch-guard-test")
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        condition_checked = threading.Event()
        state = ProviderCallState()
        action_calls = 0

        def block_lane() -> None:
            blocker_started.set()
            release_blocker.wait(timeout=1.0)

        blocker = asyncio.create_task(
            worker.run(
                block_lane,
                budget=OperationBudget.start(1.0),
                operation="hold action lane",
            )
        )
        assert await asyncio.to_thread(blocker_started.wait, 0.5)

        def condition() -> bool:
            condition_checked.set()
            return False

        def action():
            nonlocal action_calls
            action_calls += 1

        guarded = asyncio.create_task(
            run_native_action_until(
                73,
                action,
                condition,
                timeout=1,
                subscription_factory=lambda _pid: subscription,
                action_worker=worker,
                action_state=state,
                skip_action_if_condition=True,
                require_subscription_for_dispatch=True,
                abort_dispatch_on_change=True,
            )
        )
        assert await asyncio.to_thread(condition_checked.wait, 0.5)
        await asyncio.sleep(0.02)
        subscription.generation += 1
        release_blocker.set()
        await blocker

        with pytest.raises(OperationError) as exc_info:
            await guarded
        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0
        assert state.started is False
        assert state.may_have_run is False
        await worker.aclose()

    asyncio.run(run())


def test_ended_observer_is_not_accepted_as_dispatch_coverage():
    async def run():
        subscription = FakeSubscription(generation=1)
        subscription.active = False
        action_calls = 0

        def action():
            nonlocal action_calls
            action_calls += 1

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                73,
                action,
                lambda: False,
                timeout=1,
                subscription_factory=lambda _pid: subscription,
                require_subscription_for_dispatch=True,
            )

        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0

    asyncio.run(run())


def test_worker_side_pre_dispatch_check_fails_before_action_is_marked_started():
    async def run():
        subscription = FakeSubscription()
        state = ProviderCallState()
        action_calls = 0

        def action():
            nonlocal action_calls
            action_calls += 1

        def reject_dispatch() -> None:
            raise OperationError(
                OperationErrorCode.PROVIDER_BUSY,
                "browser process cohort changed",
            )

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                73,
                action,
                lambda: False,
                timeout=1,
                subscription_factory=lambda _pid: subscription,
                action_state=state,
                require_subscription_for_dispatch=True,
                pre_dispatch_check=reject_dispatch,
            )

        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0
        assert state.started is False
        assert state.may_have_run is False

    asyncio.run(run())


def test_generation_change_during_external_predispatch_check_stops_action():
    async def run():
        subscription = FakeSubscription()
        state = ProviderCallState()
        action_calls = 0

        def action():
            nonlocal action_calls
            action_calls += 1

        def change_during_check() -> None:
            subscription.generation += 1

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                73,
                action,
                lambda: False,
                timeout=1,
                subscription_factory=lambda _pid: subscription,
                action_state=state,
                require_subscription_for_dispatch=True,
                abort_dispatch_on_change=True,
                pre_dispatch_check=change_during_check,
            )

        assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
        assert action_calls == 0
        assert state.started is False
        assert state.may_have_run is False

    asyncio.run(run())


def test_native_action_watchdog_recovers_a_missed_completion_event_without_replay():
    async def run():
        subscription = FakeSubscription()
        observations = iter([False, True])
        action_calls = 0

        async def factory(pid: int):
            return subscription

        def action():
            nonlocal action_calls
            action_calls += 1
            return "activated"

        result = await run_native_action_until(
            7,
            action,
            lambda: next(observations),
            timeout=0.18,
            subscription_factory=factory,
        )

        assert result.condition_met is True
        assert result.checks == 2
        assert result.elapsed < 0.18
        assert action_calls == 1
        assert subscription.waited_after == [0]
        assert subscription.closed is True

    asyncio.run(run())


def test_action_total_deadline_includes_observer_registration_and_prevents_dispatch():
    async def run():
        subscription = FakeSubscription()
        action_calls = 0

        async def factory(pid: int):
            await asyncio.sleep(0.03)
            return subscription

        def action():
            nonlocal action_calls
            action_calls += 1
            return True

        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                7,
                action,
                lambda: True,
                timeout=0.02,
                subscription_factory=factory,
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert action_calls == 0

    asyncio.run(run())


def test_action_local_timeout_is_bounded_inside_longer_parent_budget():
    async def run():
        action_calls = 0

        async def factory(pid: int):
            await asyncio.sleep(0.04)
            return FakeSubscription()

        def action():
            nonlocal action_calls
            action_calls += 1
            return True

        started = time.monotonic()
        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                7,
                action,
                lambda: True,
                timeout=0.01,
                budget=OperationBudget.start(1.0),
                subscription_factory=factory,
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert time.monotonic() - started < 0.04
        assert action_calls == 0

    asyncio.run(run())


def test_slow_native_lookup_respects_total_deadline_and_quarantines_worker():
    async def run():
        started = threading.Event()
        release = threading.Event()
        worker = ProviderWorker("native-wait-test")

        class SlowAdapter:
            def find_elements(self, *args, **kwargs):
                started.set()
                release.wait(timeout=1.0)
                return []

        await worker.run(
            lambda: None,
            budget=OperationBudget.start(1.0),
            operation="native wait worker warmup",
        )
        before = time.monotonic()
        result = await wait_for_native_element(
            SlowAdapter(),
            42,
            role="dialog",
            timeout=0.1,
            subscription_factory=lambda pid: None,
            worker=worker,
        )
        elapsed = time.monotonic() - before

        assert result.element is None
        assert elapsed < 0.2
        assert started.is_set()
        assert worker.busy is True

        release.set()
        await worker.wait_until_idle()
        await worker.aclose()

    asyncio.run(run())


def test_slow_native_action_respects_deadline_and_quarantines_worker():
    async def run():
        started = threading.Event()
        release = threading.Event()
        worker = ProviderWorker("native-action-test")

        def slow_action():
            started.set()
            release.wait(timeout=1.0)
            return True

        await worker.run(
            lambda: None,
            budget=OperationBudget.start(1.0),
            operation="native action worker warmup",
        )
        before = time.monotonic()
        with pytest.raises(OperationError) as exc_info:
            await run_native_action_until(
                42,
                slow_action,
                lambda: True,
                timeout=0.1,
                subscription_factory=lambda pid: None,
                worker=worker,
            )
        elapsed = time.monotonic() - before

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert elapsed < 0.2
        assert started.is_set()
        assert worker.busy is True

        release.set()
        await worker.wait_until_idle()
        await worker.aclose()

    asyncio.run(run())


def test_native_action_can_dispatch_and_observe_on_distinct_affinity_lanes():
    async def run():
        action_worker = ProviderWorker("native-action-lane-test")
        condition_worker = ProviderWorker("native-condition-lane-test")
        action_thread = 0
        condition_thread = 0

        def action():
            nonlocal action_thread
            action_thread = threading.get_ident()
            return True

        def condition():
            nonlocal condition_thread
            condition_thread = threading.get_ident()
            return True

        try:
            result = await run_native_action_until(
                42,
                action,
                condition,
                timeout=1.0,
                subscription_factory=lambda pid: None,
                action_worker=action_worker,
                condition_worker=condition_worker,
            )

            assert result.condition_met is True
            assert action_thread != 0
            assert condition_thread != 0
            assert action_thread != condition_thread
        finally:
            await action_worker.aclose()
            await condition_worker.aclose()

    asyncio.run(run())


def test_slow_observer_cleanup_cannot_extend_total_deadline():
    async def run():
        deadline_consumed = threading.Event()
        registration_completed = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_completed = asyncio.Event()

        def clock() -> float:
            return 1.0 if deadline_consumed.is_set() else 0.0

        class SlowCleanupSubscription(FakeSubscription):
            async def aclose(self) -> None:
                cleanup_started.set()
                await release_cleanup.wait()
                self.closed = True
                cleanup_completed.set()

        subscription = SlowCleanupSubscription()
        budget = OperationBudget.start(1.0, clock=clock)

        async def factory(pid: int):
            registration_completed.set()
            return subscription

        def condition() -> bool:
            deadline_consumed.set()
            return True

        result = await asyncio.wait_for(
            run_native_action_until(
                42,
                lambda: True,
                condition,
                timeout=1.0,
                subscription_factory=factory,
                budget=budget,
            ),
            timeout=1.0,
        )

        assert result.condition_met is True
        assert registration_completed.is_set()
        assert budget.expired is True
        assert cleanup_started.is_set()
        assert subscription.closed is False

        release_cleanup.set()
        await asyncio.wait_for(cleanup_completed.wait(), timeout=1.0)
        assert subscription.closed is True

    asyncio.run(run())


def test_event_wait_subscribes_before_immediate_evaluation_and_cleans_up():
    async def run():
        element = UIElement(id=7, role="button", name="Continue")
        adapter = FakeAdapter([[element]])
        subscription = FakeSubscription()
        opened = False

        async def factory(pid: int):
            nonlocal opened
            assert pid == 42
            opened = True
            return subscription

        result = await wait_for_native_element(
            adapter,
            42,
            role="button",
            name="Continue",
            timeout=1,
            subscription_factory=factory,
        )

        assert opened is True
        assert result.element is element
        assert result.event_driven is True
        assert result.checks == 1
        assert subscription.waited_after == []
        assert subscription.closed is True

    asyncio.run(run())


def test_event_wait_rechecks_only_after_change_notification():
    async def run():
        element = UIElement(id=8, role="heading", name="Loaded")
        adapter = FakeAdapter([[], [element]])
        subscription = FakeSubscription(changes=[True])

        async def factory(pid: int):
            return subscription

        started = time.monotonic()
        result = await wait_for_native_element(
            adapter,
            9,
            role="heading",
            name="Loaded",
            timeout=1,
            subscription_factory=factory,
        )
        elapsed = time.monotonic() - started

        assert result.element is element
        assert adapter.calls == 2
        assert elapsed < 0.1
        assert subscription.waited_after == [0]
        assert subscription.closed is True

    asyncio.run(run())


def test_event_watchdog_recovers_when_a_partial_subscription_misses_the_change():
    async def run():
        element = UIElement(id=18, role="dialog", name="Ready")
        adapter = FakeAdapter([[], [element]])
        subscription = FakeSubscription()

        async def factory(pid: int):
            return subscription

        result = await wait_for_native_element(
            adapter,
            9,
            role="dialog",
            name="Ready",
            timeout=0.18,
            subscription_factory=factory,
        )

        assert result.element is element
        assert result.checks == 2
        assert result.elapsed < 0.18
        assert adapter.calls == 2
        assert subscription.waited_after == [0]
        assert subscription.closed is True

    asyncio.run(run())


def test_event_during_initial_evaluation_is_not_lost():
    async def run():
        element = UIElement(id=9, role="alert", name="Ready")
        subscription = FakeSubscription()

        def on_find(call: int):
            if call == 1:
                subscription.generation += 1

        adapter = FakeAdapter([[], [element]], on_find=on_find)

        async def factory(pid: int):
            return subscription

        result = await wait_for_native_element(
            adapter,
            10,
            role="alert",
            timeout=1,
            subscription_factory=factory,
        )

        assert result.element is element
        assert adapter.calls == 2
        assert subscription.waited_after == [0]

    asyncio.run(run())


def test_event_wait_timeout_is_precise_and_does_not_requery_without_event():
    async def run():
        adapter = FakeAdapter([[]])
        subscription = FakeSubscription()

        async def factory(pid: int):
            return subscription

        started = time.monotonic()
        result = await wait_for_native_element(
            adapter,
            11,
            role="dialog",
            timeout=0.03,
            subscription_factory=factory,
        )
        elapsed = time.monotonic() - started

        assert result.element is None
        assert result.event_driven is True
        assert adapter.calls == 1
        assert 0.02 <= elapsed < 0.15
        assert subscription.closed is True

    asyncio.run(run())


def test_event_watchdog_query_rate_is_bounded_when_timer_fires_early(monkeypatch):
    async def run():
        timeout = 0.16
        adapter = FakeAdapter([[]])
        subscription = NativeChangeSubscription(11)
        subscription._loop = asyncio.get_running_loop()
        subscription._wake = asyncio.Event()
        original_sleep = asyncio.sleep
        positive_sleeps = 0

        async def sleep_early_twice(delay: float):
            nonlocal positive_sleeps
            if delay > 0 and positive_sleeps < 2:
                positive_sleeps += 1
                await original_sleep(0)
                return
            await original_sleep(delay)

        monkeypatch.setattr("agent_eyes.native_events.asyncio.sleep", sleep_early_twice)

        async def factory(pid: int):
            return subscription

        result = await asyncio.wait_for(
            wait_for_native_element(
                adapter,
                11,
                role="dialog",
                timeout=timeout,
                subscription_factory=factory,
            ),
            timeout=1.0,
        )

        assert result.element is None
        assert result.event_driven is True
        assert result.checks == 2
        assert adapter.calls == 2
        assert positive_sleeps == 1
        assert 0.14 <= result.elapsed < 0.3
        assert subscription._closed is True

    asyncio.run(run())


def test_native_subscription_retries_an_early_asyncio_timeout(monkeypatch):
    async def run():
        subscription = NativeChangeSubscription(12)
        subscription._loop = asyncio.get_running_loop()
        subscription._wake = asyncio.Event()
        original_wait_for = asyncio.wait_for
        wait_calls = 0

        async def wait_for_with_one_early_timeout(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                awaitable.close()
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout)

        monkeypatch.setattr(
            "agent_eyes.native_events.asyncio.wait_for",
            wait_for_with_one_early_timeout,
        )
        asyncio.get_running_loop().call_soon(subscription._signal_change)

        changed = await subscription.wait_for_change(0, timeout=0.1)

        assert changed is True
        assert wait_calls == 2
        assert subscription.generation == 1

    asyncio.run(run())


def test_cancelling_event_wait_closes_subscription():
    async def run():
        adapter = FakeAdapter([[]])
        subscription = FakeSubscription()

        async def factory(pid: int):
            return subscription

        task = asyncio.create_task(
            wait_for_native_element(
                adapter,
                12,
                role="button",
                timeout=10,
                subscription_factory=factory,
            )
        )
        while not subscription.waited_after:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert subscription.closed is True

    asyncio.run(run())


def test_cancelling_registration_closes_partially_started_subscription(monkeypatch):
    async def run():
        from agent_eyes import native_events

        class SlowSubscription:
            def __init__(self):
                self.started = asyncio.Event()
                self.closed = False

            async def start(self, timeout: float):
                self.started.set()
                await asyncio.sleep(10)

            async def aclose(self):
                self.closed = True

        subscription = SlowSubscription()
        monkeypatch.setattr(
            native_events,
            "create_native_change_subscription",
            lambda pid: subscription,
        )

        task = asyncio.create_task(open_native_change_subscription(22))
        await subscription.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert subscription.closed is True

    asyncio.run(run())


def test_unavailable_events_use_bounded_adaptive_fallback():
    async def run():
        element = UIElement(id=13, role="button", name="Done")
        adapter = FakeAdapter([[], [], [element]])

        async def factory(pid: int):
            return None

        result = await wait_for_native_element(
            adapter,
            13,
            role="button",
            name="Done",
            timeout=0.5,
            subscription_factory=factory,
            fallback_initial_interval=0.001,
            fallback_max_interval=0.002,
        )

        assert result.element is element
        assert result.event_driven is False
        assert result.checks == 3

    asyncio.run(run())


def test_registration_failure_uses_adaptive_fallback():
    async def run():
        element = UIElement(id=15, role="button", name="Recovered")
        adapter = FakeAdapter([[element]])

        async def factory(pid: int):
            raise ValueError("mock platform registration failure")

        result = await wait_for_native_element(
            adapter,
            15,
            role="button",
            timeout=0.1,
            subscription_factory=factory,
        )

        assert result.element is element
        assert result.event_driven is False
        assert adapter.calls == 1

    asyncio.run(run())


def test_zero_timeout_does_not_dispatch_a_provider_query():
    async def run():
        element = UIElement(id=14, role="button", name="Now")
        adapter = FakeAdapter([[element]])

        async def factory(pid: int):
            return None

        result = await wait_for_native_element(
            adapter,
            14,
            role="button",
            timeout=0,
            subscription_factory=factory,
        )

        assert result.element is None
        assert adapter.calls == 0

    asyncio.run(run())


def test_subscription_generation_check_has_no_clear_then_wait_race():
    async def run():
        subscription = NativeChangeSubscription(15)
        subscription._loop = asyncio.get_running_loop()
        subscription._wake = asyncio.Event()

        subscription._signal_change()
        changed = await subscription.wait_for_change(0, timeout=0.01)

        assert changed is True
        assert subscription.generation == 1

    asyncio.run(run())


def test_factory_selects_windows_and_linux_backends_without_importing_other_platforms():
    windows = create_native_change_subscription(20, platform="win32")
    linux = create_native_change_subscription(21, platform="linux")

    assert isinstance(windows, WindowsUIAChangeSubscription)
    assert isinstance(linux, LinuxATSPIChangeSubscription)


def test_macos_backend_rejects_partial_mutation_critical_coverage(monkeypatch):
    calls: list[tuple] = []
    observer = object()
    application = object()

    application_services = SimpleNamespace(
        AXObserverCreate=lambda pid, callback, refcon: (0, observer),
        AXUIElementCreateApplication=lambda pid: application,
        AXObserverAddNotification=lambda current_observer, element, notification, refcon: (
            1 if notification == "kAXCreatedNotification" else 0
        ),
        AXObserverRemoveNotification=lambda current_observer, element, notification: (
            calls.append(("remove", notification)) or 0
        ),
    )
    for name in MacOSAXChangeSubscription._NOTIFICATION_NAMES:
        setattr(application_services, name, name)
    objc = SimpleNamespace(callbackFor=lambda function: lambda callback: callback)
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    monkeypatch.setitem(sys.modules, "objc", objc)

    subscription = MacOSAXChangeSubscription(73)
    subscription._run_backend_for_test()

    assert subscription._available is False
    assert ("remove", "kAXCreatedNotification") not in calls
    assert ("remove", "kAXWindowCreatedNotification") in calls


def test_windows_backend_registers_structure_and_content_events_with_mock_runtime():
    calls: list[tuple] = []
    target = object()
    root = SimpleNamespace()

    class Collection:
        Length = 1

        def GetElement(self, index: int):
            assert index == 0
            return target

    class Automation:
        def GetRootElement(self):
            root.FindAll = lambda scope, condition: Collection()
            root.FindFirst = lambda scope, condition: target
            return root

        def CreatePropertyCondition(self, property_id, pid):
            calls.append(("condition", property_id, pid))
            return object()

        def AddStructureChangedEventHandler(self, element, scope, cache, handler):
            calls.append(("add_structure", element, scope, cache, handler))

        def RemoveStructureChangedEventHandler(self, element, handler):
            calls.append(("remove_structure", element, handler))

        def AddAutomationEventHandler(self, event_id, element, scope, cache, handler):
            calls.append(("add_event", event_id, element, scope, cache, handler))

        def RemoveAutomationEventHandler(self, event_id, element, handler):
            calls.append(("remove_event", event_id, element, handler))

        def AddPropertyChangedEventHandlerNativeArray(
            self, element, scope, cache, handler, property_ids, property_count
        ):
            calls.append(
                (
                    "add_property",
                    element,
                    scope,
                    cache,
                    handler,
                    tuple(property_ids),
                    property_count,
                )
            )

        def RemovePropertyChangedEventHandler(self, element, handler):
            calls.append(("remove_property", element, handler))

    runtime = SimpleNamespace(
        automation=Automation(),
        process_id_property=30002,
        tree_scope_children=2,
        tree_scope_descendants=4,
        tree_scope_subtree=7,
        event_ids=(20008, 20015, 20024),
        global_event_ids=(20016,),
        property_ids=(30003, 30005, 30010, 30022, 30045),
        make_property_array=lambda property_ids: tuple(property_ids),
        make_structure_handler=lambda callback: SimpleNamespace(callback=callback),
        make_event_handler=lambda callback: SimpleNamespace(callback=callback),
        make_property_handler=lambda callback: SimpleNamespace(callback=callback),
        wait_until_stopped=lambda stop_event: stop_event.wait(0.01),
        close=lambda: calls.append(("runtime_close",)),
    )
    subscription = WindowsUIAChangeSubscription(99, runtime_factory=lambda: runtime)
    subscription._run_backend_for_test()

    assert ("condition", 30002, 99) in calls
    assert any(call[0] == "add_structure" for call in calls)
    assert [call[1] for call in calls if call[0] == "add_event"] == [
        20016,
        20008,
        20015,
        20024,
    ]
    assert [call[5] for call in calls if call[0] == "add_property"] == [
        (30003, 30005, 30010, 30022, 30045)
    ]
    assert any(call[0] == "remove_structure" for call in calls)
    assert any(call[0] == "remove_property" for call in calls)
    assert [call[1] for call in calls if call[0] == "remove_event"] == [
        20024,
        20015,
        20008,
        20016,
    ]
    global_handler = next(
        call[5] for call in calls if call[0] == "add_event" and call[1] == 20016
    )
    global_handler.callback(SimpleNamespace(CurrentProcessId=100), 20016)
    assert subscription.generation == 0
    global_handler.callback(SimpleNamespace(CurrentProcessId=99), 20016)
    assert subscription.generation == 1
    assert calls[-1] == ("runtime_close",)


def test_windows_backend_rejects_partial_mutation_critical_coverage():
    calls: list[tuple] = []
    target = object()

    class Collection:
        Length = 1

        def GetElement(self, index: int):
            assert index == 0
            return target

    class Root:
        def FindAll(self, scope, condition):
            return Collection()

        def FindFirst(self, scope, condition):
            return target

    class Automation:
        def GetRootElement(self):
            return Root()

        def CreatePropertyCondition(self, property_id, pid):
            return object()

        def AddAutomationEventHandler(self, *args):
            calls.append(("event", args[0]))

        def AddStructureChangedEventHandler(self, *args):
            raise RuntimeError("structure events unavailable")

        def AddPropertyChangedEventHandlerNativeArray(self, *args):
            calls.append(("property",))

        def RemoveAutomationEventHandler(self, *args):
            pass

        def RemovePropertyChangedEventHandler(self, *args):
            pass

    runtime = SimpleNamespace(
        automation=Automation(),
        process_id_property=30002,
        tree_scope_children=2,
        tree_scope_descendants=4,
        tree_scope_subtree=7,
        event_ids=(20008,),
        global_event_ids=(20016,),
        property_ids=(30003, 30005),
        make_property_array=lambda property_ids: tuple(property_ids),
        make_structure_handler=lambda callback: object(),
        make_event_handler=lambda callback: object(),
        make_property_handler=lambda callback: object(),
        wait_until_stopped=lambda stop_event: calls.append(("wait",)),
        close=lambda: calls.append(("close",)),
    )

    subscription = WindowsUIAChangeSubscription(99, runtime_factory=lambda: runtime)
    subscription._run_backend_for_test()

    assert subscription._available is False
    assert ("wait",) not in calls
    assert calls[-1] == ("close",)


def test_linux_backend_filters_events_to_pid_and_deregisters_mock_listener():
    calls: list[tuple] = []

    class Source:
        def get_process_id(self):
            return 77

    class Listener:
        def register_with_app(self, event_type, properties, app):
            calls.append(("register", event_type, properties, app))
            return True

        def deregister(self, event_type):
            calls.append(("deregister", event_type))
            return True

    listener = Listener()
    main_loop = SimpleNamespace(
        run=lambda: calls.append(("run",)),
        quit=lambda: calls.append(("quit",)),
    )
    app = object()
    captured_callback = None

    def make_listener(callback):
        nonlocal captured_callback
        captured_callback = callback
        return listener

    runtime = SimpleNamespace(
        find_application=lambda pid: app if pid == 77 else None,
        make_listener=make_listener,
        make_main_loop=lambda: main_loop,
        idle_add=lambda callback: callback(),
        event_types=(
            "object:children-changed",
            "object:property-change:accessible-name",
            "object:property-change:accessible-value",
            "window:create",
        ),
    )
    subscription = LinuxATSPIChangeSubscription(77, runtime_factory=lambda: runtime)
    subscription._run_backend_for_test()

    assert captured_callback is not None
    captured_callback(SimpleNamespace(source=Source()))
    assert subscription.generation == 1
    assert calls[:4] == [
        ("register", "object:children-changed", None, app),
        ("register", "object:property-change:accessible-name", None, app),
        ("register", "object:property-change:accessible-value", None, app),
        ("register", "window:create", None, app),
    ]
    assert ("run",) in calls
    assert calls[-4:] == [
        ("deregister", "window:create"),
        ("deregister", "object:property-change:accessible-value"),
        ("deregister", "object:property-change:accessible-name"),
        ("deregister", "object:children-changed"),
    ]


def test_linux_backend_rejects_partial_mutation_critical_coverage():
    calls: list[tuple] = []
    app = object()

    class Listener:
        def register_with_app(self, event_type, properties, application):
            calls.append(("register", event_type))
            return event_type != "object:children-changed"

        def deregister(self, event_type):
            calls.append(("deregister", event_type))

    runtime = SimpleNamespace(
        find_application=lambda pid: app,
        make_listener=lambda callback: Listener(),
        make_main_loop=lambda: SimpleNamespace(
            run=lambda: calls.append(("run",)),
            quit=lambda: None,
        ),
        idle_add=lambda callback: callback(),
        event_types=(
            "object:children-changed",
            "object:property-change:accessible-name",
            "object:property-change:accessible-value",
            "window:create",
        ),
    )

    subscription = LinuxATSPIChangeSubscription(77, runtime_factory=lambda: runtime)
    subscription._run_backend_for_test()

    assert subscription._available is False
    assert ("run",) not in calls
