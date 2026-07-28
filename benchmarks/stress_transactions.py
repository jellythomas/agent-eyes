"""Deterministic cancellation, queue, and retention gates for transactions."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import threading
import weakref
from dataclasses import dataclass

from agent_eyes.action_kernel import ActionDispatchResult, ActionPorts
from agent_eyes.adapters.base import UIElement
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.locators import LocatorIndex
from agent_eyes.native_events import NativeChangeSubscription, run_native_action_until
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationBudget, OperationMode
from agent_eyes.provider_worker import ProviderWorker
from agent_eyes.target_resolver import TargetResolver
from agent_eyes.transaction_contract import (
    TargetMode,
    TargetSpec,
    parse_execute_request,
)
from agent_eyes.transactions import (
    TransactionEngine,
    TransactionPorts,
    TransactionStatus,
    TransactionTarget,
    TransactionView,
)
from benchmarks.benchmark_release_transactions import (
    _browser_targets,
    _foreground_queue_evidence,
)
from benchmarks.stress_concurrency import _snapshot_rss_cycles


_DEFAULT_CYCLES = 10_000
_THREADED_SUBSCRIPTION_CYCLES = 32
_CANCELLATION_BOUNDARIES = (
    "target_resolution",
    "initial_observation",
    "capability_preflight",
    "focus_preflight",
    "provider_dispatch",
    "scoped_refresh",
    "final_expectation",
)
_PRE_DISPATCH_BOUNDARIES = frozenset(_CANCELLATION_BOUNDARIES[:4])


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


@dataclass(slots=True)
class _BoundaryGate:
    entered: asyncio.Event
    release: asyncio.Event

    @classmethod
    def create(cls) -> _BoundaryGate:
        return cls(asyncio.Event(), asyncio.Event())

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


def _boundary_request(boundary: str):
    click: dict[str, object] = {"op": "click", "ref": "save"}
    arguments: dict[str, object] = {
        "target": {"pid": 73},
        "steps": [
            {
                "op": "locate",
                "as": "save",
                "role": "button",
                "name": "Save",
            },
            click,
        ],
        "deadline_ms": 3_000,
    }
    if boundary == "scoped_refresh":
        click["expect"] = {"role": "status", "name": "Saved"}
    elif boundary == "final_expectation":
        arguments["expect"] = {"role": "article", "name": "Posted"}
    return parse_execute_request(arguments)


async def _cancel_at_boundary(boundary: str) -> dict[str, object]:
    gate = _BoundaryGate.create()
    counters = {"dispatches": 0, "refreshes": 0}
    button = UIElement(id=1, role="button", name="Save")

    async def maybe_block(name: str) -> None:
        if boundary == name:
            await gate.wait()

    async def resolve(_spec, _activate, _budget):
        await maybe_block("target_resolution")
        return TransactionTarget(target_id="pid:73")

    async def observe(_target, _budget):
        await maybe_block("initial_observation")
        return TransactionView(
            index=LocatorIndex.from_roots(button),
            snapshot="n-initial",
        )

    async def refresh(_target, _current, _locator, _budget):
        counters["refreshes"] += 1
        if boundary in {"scoped_refresh", "final_expectation"}:
            await gate.wait()
        return TransactionView(
            index=LocatorIndex.from_roots(
                UIElement(id=2, role="status", name="Saved"),
                UIElement(id=3, role="article", name="Posted"),
            ),
            snapshot="n-refreshed",
        )

    def action_ports(_step, _element, _target):
        async def capability(_budget):
            await maybe_block("capability_preflight")
            return True

        async def focus(_budget):
            await maybe_block("focus_preflight")
            return True

        async def dispatch(_budget):
            counters["dispatches"] += 1
            await maybe_block("provider_dispatch")
            return ActionDispatchResult.succeeded(changed=True)

        return ActionPorts(
            provider_code="native.stress",
            capability=capability,
            focus=focus,
            dispatch=dispatch,
        )

    task = asyncio.create_task(
        TransactionEngine().run(
            _boundary_request(boundary),
            ports=TransactionPorts(
                resolve=resolve,
                observe=observe,
                refresh=refresh,
                action_ports=action_ports,
            ),
        )
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=1.0)
        task.cancel()
        try:
            result = await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            terminal = "cancelled"
            completed_steps = None
            failed_step = None
            retry_safe = None
        else:
            if result.status is not TransactionStatus.OUTCOME_UNKNOWN:
                raise RuntimeError(
                    f"post-dispatch cancellation at {boundary} was not uncertain"
                )
            terminal = result.status.value
            completed_steps = result.completed_steps
            failed_step = result.failed_step
            retry_safe = result.retry_safe
    finally:
        gate.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    expected_terminal = (
        "cancelled" if boundary in _PRE_DISPATCH_BOUNDARIES else "outcome_unknown"
    )
    expected_dispatches = 0 if boundary in _PRE_DISPATCH_BOUNDARIES else 1
    if terminal != expected_terminal or counters["dispatches"] != expected_dispatches:
        raise RuntimeError(f"cancellation invariant failed at {boundary}")
    return {
        "terminal": terminal,
        "dispatches": counters["dispatches"],
        "refreshes": counters["refreshes"],
        "completed_steps": completed_steps,
        "failed_step": failed_step,
        "retry_safe": retry_safe,
    }


async def _cancellation_boundary_evidence() -> dict[str, dict[str, object]]:
    """Cancel only after each named transaction boundary proves it was entered."""
    return {
        boundary: await _cancel_at_boundary(boundary)
        for boundary in _CANCELLATION_BOUNDARIES
    }


class _ProviderReference:
    def __init__(self) -> None:
        self.payload = bytearray(4 * 1024)
        self.payload[0] = 1


class _TrackedSubscription:
    def __init__(self, tracker: dict[str, int]) -> None:
        self._tracker = tracker
        self.generation = 0
        self.active = True
        tracker["opened"] += 1

    async def wait_for_change(self, _after_generation: int, _timeout: float) -> bool:
        return False

    async def aclose(self) -> None:
        if self.active:
            self.active = False
            self._tracker["closed"] += 1


class _ThreadedStressSubscription(NativeChangeSubscription):
    """Exercise the production thread/start/close lifecycle without an OS binding."""

    def _run_backend(self) -> None:
        self._mark_ready(True)
        self._stop_requested.wait()


def _worker_is_clean(worker: ProviderWorker) -> bool:
    with worker._guard:
        return bool(
            not worker._waiters
            and worker._handoff is None
            and worker._active is None
            and not worker._occupied
        )


async def _resource_retention_evidence(
    cycles: int = _DEFAULT_CYCLES,
) -> dict[str, int | bool]:
    """Run 10k lifecycle cycles and report exact retained-resource counts."""
    if cycles < 1:
        raise ValueError("cycles must be positive")

    token_index = 0
    released_references = 0
    provider_references: weakref.WeakSet[_ProviderReference] = weakref.WeakSet()
    live_subscriptions: weakref.WeakSet[_TrackedSubscription] = weakref.WeakSet()
    threaded_references: list[weakref.ReferenceType[_ThreadedStressSubscription]] = []
    subscription_tracker = {"opened": 0, "closed": 0}

    def token_factory() -> str:
        nonlocal token_index
        token_index += 1
        return f"transaction-stress-{token_index}"

    def release_reference(value: object) -> None:
        nonlocal released_references
        if not isinstance(value, _ProviderReference):
            raise RuntimeError("retention stress released an unexpected reference")
        released_references += 1

    store = ObservationStore(max_snapshots=4, token_factory=token_factory)
    coordinator = AutomationCoordinator(store)
    action_worker = ProviderWorker("transaction-stress-action")
    condition_worker = ProviderWorker("transaction-stress-condition")
    action_worker_reference = weakref.ref(action_worker)
    condition_worker_reference = weakref.ref(condition_worker)
    provider_identity = object()
    adapter_identity = object()
    inventory_calls = 0
    _targets, browser_target = _browser_targets()

    async def inventory(_provider, _adapter, _mode):
        nonlocal inventory_calls
        inventory_calls += 1
        return (browser_target,)

    async def activate(_provider, _adapter, _target):
        raise AssertionError("retention inspection must not activate")

    resolver = TargetResolver(inventory, activate)
    target_spec = TargetSpec(
        mode=TargetMode.FOREGROUND,
        query="BIT-482 pagination guard pull request",
    )

    async def observed_value() -> str:
        return "observed"

    async def shadow_value() -> str:
        return "mutated"

    def subscription_factory(_pid: int) -> _TrackedSubscription:
        subscription = _TrackedSubscription(subscription_tracker)
        live_subscriptions.add(subscription)
        return subscription

    try:
        for revision in range(cycles):
            provider_reference = _ProviderReference()
            provider_references.add(provider_reference)
            store.create(
                provider="native",
                mode=OperationMode.FOREGROUND,
                target_id="pid:73",
                generation=0,
                revision=revision,
                elements=[ElementRecord(1, provider_reference)],
                release=release_reference,
            )
            del provider_reference
            if (
                store.invalidate_target(
                    provider="native",
                    mode=OperationMode.FOREGROUND,
                    target_id="pid:73",
                )
                != 1
            ):
                raise RuntimeError("snapshot lifecycle did not invalidate exactly once")

            if (
                await coordinator.observe(("cycle", revision), observed_value)
                != "observed"
            ):
                raise RuntimeError("coordinator observation returned the wrong value")
            if (
                await coordinator.execute_shadow(("target", revision), shadow_value)
                != "mutated"
            ):
                raise RuntimeError(
                    "coordinator shadow operation returned the wrong value"
                )

            await resolver.resolve(
                target_spec,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
            )
            resolver.invalidate(
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=TargetMode.FOREGROUND,
            )

            action_result = await run_native_action_until(
                73,
                lambda: True,
                lambda: True,
                timeout=1.0,
                budget=OperationBudget.start(1.0),
                subscription_factory=subscription_factory,
                action_worker=action_worker,
                condition_worker=condition_worker,
            )
            if not action_result.condition_met or not action_result.action_dispatched:
                raise RuntimeError("native action lifecycle did not complete once")

        await action_worker.wait_until_idle()
        await condition_worker.wait_until_idle()
        workers_clean_before_close = _worker_is_clean(
            action_worker
        ) and _worker_is_clean(condition_worker)

        for index in range(_THREADED_SUBSCRIPTION_CYCLES):
            subscription = _ThreadedStressSubscription(20_000 + index)
            threaded_references.append(weakref.ref(subscription))
            if not await subscription.start(timeout=1.0):
                raise RuntimeError("threaded subscription did not become active")
            await subscription.aclose()
            if subscription.active:
                raise RuntimeError("threaded subscription remained active after close")
            del subscription
    finally:
        await coordinator.close()
        await asyncio.gather(
            action_worker.aclose(),
            condition_worker.aclose(),
        )

    resolver_cache_entries = len(resolver._cache)
    resolver_lease_entries = sum(
        len(bucket) for bucket in resolver._exact_leases.values()
    )
    resolver_flights = len(resolver._flights)
    coordinator_flights = len(coordinator._flights)
    coordinator_shadow_locks = len(coordinator._shadow_locks)
    coordinator_poison = len(coordinator._foreground_poison) + sum(
        len(tasks) for tasks in coordinator._shadow_poison.values()
    )
    foreground_locked = coordinator._foreground_lock.locked()
    snapshots = len(store._snapshots)
    owned_event_threads = sum(
        thread.name.startswith("agent-eyes-native-events-")
        for thread in threading.enumerate()
    )

    del action_worker
    del condition_worker
    gc.collect()
    return {
        "cycles": cycles,
        "released_references": released_references,
        "live_provider_references": len(provider_references),
        "opened_subscriptions": subscription_tracker["opened"],
        "closed_subscriptions": subscription_tracker["closed"],
        "live_subscriptions": len(live_subscriptions),
        "threaded_subscription_cycles": _THREADED_SUBSCRIPTION_CYCLES,
        "live_threaded_subscriptions": sum(
            reference() is not None for reference in threaded_references
        ),
        "native_event_threads": owned_event_threads,
        "workers_clean_before_close": workers_clean_before_close,
        "live_workers_after_close": int(action_worker_reference() is not None)
        + int(condition_worker_reference() is not None),
        "snapshots": snapshots,
        "resolver_cache_entries": resolver_cache_entries,
        "resolver_lease_entries": resolver_lease_entries,
        "resolver_flights": resolver_flights,
        "coordinator_flights": coordinator_flights,
        "coordinator_shadow_locks": coordinator_shadow_locks,
        "coordinator_poison": coordinator_poison,
        "foreground_locked": foreground_locked,
        "inventory_calls": inventory_calls,
    }


async def _stress(cycles: int = _DEFAULT_CYCLES) -> dict[str, object]:
    cancellations = await _cancellation_boundary_evidence()
    queue = await _foreground_queue_evidence(concurrency=32, cancellation_count=16)
    resources = await _resource_retention_evidence(cycles)
    snapshot_rss = _snapshot_rss_cycles(cycles)
    return {
        "schema_version": 1,
        "cancellation_boundaries": cancellations,
        "foreground_queue": queue,
        "resources": resources,
        "snapshot_rss": snapshot_rss,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=_positive_integer, default=_DEFAULT_CYCLES)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(_stress(arguments.cycles)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
