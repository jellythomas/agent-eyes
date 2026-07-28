"""Deterministic release evidence for public Agent Eyes transactions."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
from types import SimpleNamespace
from typing import Any
import weakref

from mcp.types import CallToolResult

from agent_eyes import __version__
from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.locators import LocatorIndex
from agent_eyes.native_events import run_native_action_until
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationMode


_DEFAULT_CONCURRENCY = 32
_DEFAULT_CANCELLATIONS = 16
_DEFAULT_SNAPSHOT_CYCLES = 64
_DEFAULT_TREE_DEPTH = 384
_DEFAULT_SNAPSHOT_CAPACITY = 4
_DEFAULT_PAYLOAD_BYTES = 512
_MAX_TREE_DEPTH = 500


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _text(result: Any) -> str:
    content = result.content if isinstance(result, CallToolResult) else result
    return "\n".join(item.text for item in content)


def _payload(result: Any) -> dict[str, Any]:
    if isinstance(result, CallToolResult) and result.isError:
        raise RuntimeError(f"public execute returned an MCP error: {_text(result)}")
    payload = json.loads(_text(result))
    if payload.get("status") != "succeeded":
        raise RuntimeError("public execute did not report success")
    return payload


class _InlineWorker:
    async def run(self, call, **_kwargs):
        return call()

    async def wait_until_idle(self) -> None:
        return None


class _QueueEvidenceCoordinator(AutomationCoordinator):
    def __init__(self, concurrency: int) -> None:
        super().__init__()
        self._concurrency = concurrency
        self.all_entered = asyncio.Event()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.entered = 0
        self.started = 0
        self.completed = 0
        self.active = 0
        self.max_active = 0

    async def execute_foreground(
        self,
        operation,
        *,
        budget=None,
        operation_manages_deadline: bool = False,
    ):
        ordinal = self.entered
        self.entered += 1
        if self.entered == self._concurrency:
            self.all_entered.set()

        async def tracked_operation():
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if ordinal == 0:
                    self.first_started.set()
                    await self.release_first.wait()
                return await operation()
            finally:
                self.active -= 1
                self.completed += 1

        return await super().execute_foreground(
            tracked_operation,
            budget=budget,
            operation_manages_deadline=operation_manages_deadline,
        )


class _QueueAdapter:
    def __init__(self) -> None:
        self.tree_reads: dict[int, int] = {}
        self.dispatches: dict[int, int] = {}

    def get_tree(self, pid: int, max_depth: int = 10) -> UIElement:
        if max_depth != 10:
            raise AssertionError("release queue tree depth changed")
        self.tree_reads[pid] = self.tree_reads.get(pid, 0) + 1
        return UIElement(
            id=1,
            role="window",
            name=f"Queue target {pid}",
            platform_ref=object(),
            children=[
                UIElement(
                    id=2,
                    role="button",
                    name="Apply",
                    actions=["press"],
                    platform_ref=object(),
                )
            ],
        )

    def get_subtree(self, _element: UIElement, max_depth: int = 10) -> UIElement:
        raise AssertionError(
            f"one-step queue fixture must not refresh a subtree at depth {max_depth}"
        )

    def is_element_valid(self, element: UIElement) -> bool:
        return element.platform_ref is not None

    def perform_action(self, element: UIElement, action: str) -> bool:
        if action != "press" or element.pid < 1:
            return False
        self.dispatches[element.pid] = self.dispatches.get(element.pid, 0) + 1
        return True

    def list_apps(self):
        raise AssertionError("PID transactions must bypass browser inventory")


class _QueueInput:
    def is_available(self) -> bool:
        return True

    def is_frontmost(self, pid: int) -> bool:
        return pid > 0


def _queue_arguments(pid: int) -> dict[str, Any]:
    return {
        "target": {"pid": pid},
        "steps": [
            {
                "op": "locate",
                "as": "apply",
                "role": "button",
                "name": "Apply",
            },
            {
                "op": "click",
                "ref": "apply",
                "consequence": "external_write",
            },
        ],
        "deadline_ms": 30_000,
    }


async def _foreground_queue_evidence(
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    cancellation_count: int = _DEFAULT_CANCELLATIONS,
) -> dict[str, int | bool]:
    """Prove public foreground calls serialize and cancelled waiters never dispatch."""
    if concurrency < 2:
        raise ValueError("concurrency must be at least 2")
    if not 0 < cancellation_count < concurrency:
        raise ValueError("cancellation_count must be between 1 and concurrency - 1")

    from agent_eyes import server

    adapter = _QueueAdapter()
    coordinator = _QueueEvidenceCoordinator(concurrency)
    inline_worker = _InlineWorker()

    async def deterministic_events(*args, **kwargs):
        kwargs["subscription_factory"] = lambda _pid: None
        return await run_native_action_until(*args, **kwargs)

    saved = {
        "native_adapter": server.native_adapter,
        "native_worker": server.native_worker,
        "input_worker": server.input_worker,
        "input_backend": server._input_backend,
        "runtime_readiness": server._runtime_readiness,
        "resolver": server._transaction_target_resolver,
        "coordinator": server.coordinator,
        "native_events": server.run_native_action_until,
        "telemetry": server._transaction_telemetry,
        "dispatch": server._DISPATCH_TABLE,
    }
    server.native_adapter = adapter
    server.native_worker = inline_worker
    server.input_worker = inline_worker
    server._input_backend = _QueueInput()
    server._runtime_readiness = SimpleNamespace(core_ready=True)
    server._transaction_target_resolver = None
    server.coordinator = coordinator
    server.run_native_action_until = deterministic_events
    server._transaction_telemetry = None
    server._DISPATCH_TABLE = None

    tasks: list[asyncio.Task[dict[str, Any]]] = []
    cancelled_indices = set(range(1, cancellation_count + 1))

    async def invoke(index: int) -> dict[str, Any]:
        return _payload(
            await server.call_tool("execute", _queue_arguments(10_000 + index))
        )

    try:
        tasks.append(asyncio.create_task(invoke(0)))
        await coordinator.first_started.wait()
        tasks.extend(
            asyncio.create_task(invoke(index)) for index in range(1, concurrency)
        )
        await coordinator.all_entered.wait()

        for index in cancelled_indices:
            tasks[index].cancel()
        cancelled_results = await asyncio.gather(
            *(tasks[index] for index in cancelled_indices),
            return_exceptions=True,
        )
        if not all(
            isinstance(result, asyncio.CancelledError) for result in cancelled_results
        ):
            raise RuntimeError("a queued cancellation did not propagate")

        coordinator.release_first.set()
        survivor_indices = [
            index for index in range(concurrency) if index not in cancelled_indices
        ]
        completed = await asyncio.gather(*(tasks[index] for index in survivor_indices))
        if any(payload.get("completed_steps") != 2 for payload in completed):
            raise RuntimeError("a surviving transaction did not complete both steps")

        cancelled_pids = {10_000 + index for index in cancelled_indices}
        cancelled_dispatches = sum(
            count for pid, count in adapter.dispatches.items() if pid in cancelled_pids
        )
        duplicate_dispatches = sum(
            max(0, count - 1) for count in adapter.dispatches.values()
        )
        return {
            "concurrency": concurrency,
            "cancelled": cancellation_count,
            "completed": len(completed),
            "entered_queue": coordinator.entered,
            "started_operations": coordinator.started,
            "completed_operations": coordinator.completed,
            "max_active_operations": coordinator.max_active,
            "tree_reads": sum(adapter.tree_reads.values()),
            "dispatches": sum(adapter.dispatches.values()),
            "duplicate_dispatches": duplicate_dispatches,
            "cancelled_dispatches": cancelled_dispatches,
            "queue_locked_after": coordinator._foreground_lock.locked(),
        }
    finally:
        coordinator.release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.close()
        server.native_adapter = saved["native_adapter"]
        server.native_worker = saved["native_worker"]
        server.input_worker = saved["input_worker"]
        server._input_backend = saved["input_backend"]
        server._runtime_readiness = saved["runtime_readiness"]
        server._transaction_target_resolver = saved["resolver"]
        server.coordinator = saved["coordinator"]
        server.run_native_action_until = saved["native_events"]
        server._transaction_telemetry = saved["telemetry"]
        server._DISPATCH_TABLE = saved["dispatch"]


class _DeepPayload:
    __slots__ = ("buffer", "__weakref__")

    def __init__(self, size: int) -> None:
        self.buffer = bytearray(size)
        self.buffer[0] = 1


def _deep_tree_records(
    depth: int,
    payload_bytes: int,
    payload_references: list[weakref.ReferenceType[_DeepPayload]],
) -> tuple[ElementRecord, ...]:
    child: UIElement | None = None
    for local_id in reversed(range(depth)):
        payload = _DeepPayload(payload_bytes)
        payload_references.append(weakref.ref(payload))
        child = UIElement(
            id=local_id,
            role="button" if local_id == depth - 1 else "group",
            name=f"depth-{local_id}",
            platform_ref=payload,
            children=[] if child is None else [child],
        )
    if child is None:
        raise AssertionError("deep tree requires at least one element")
    index = LocatorIndex.from_roots(child)
    if len(index.elements) != depth:
        raise RuntimeError("deep locator index did not retain the complete fixture")
    return tuple(ElementRecord(element.id, element) for element in index.elements)


def _snapshot_retention_evidence(
    *,
    cycles: int = _DEFAULT_SNAPSHOT_CYCLES,
    tree_depth: int = _DEFAULT_TREE_DEPTH,
    capacity: int = _DEFAULT_SNAPSHOT_CAPACITY,
    payload_bytes: int = _DEFAULT_PAYLOAD_BYTES,
) -> dict[str, int]:
    """Measure exact retained provider bytes as a deterministic RSS proxy."""
    if cycles < capacity or capacity < 1:
        raise ValueError("cycles must be at least the positive snapshot capacity")
    if not 1 <= tree_depth <= _MAX_TREE_DEPTH:
        raise ValueError(f"tree_depth must be between 1 and {_MAX_TREE_DEPTH}")
    if payload_bytes < 1:
        raise ValueError("payload_bytes must be positive")

    token_index = 0
    released_records = 0
    payload_references: list[weakref.ReferenceType[_DeepPayload]] = []

    def token_factory() -> str:
        nonlocal token_index
        token_index += 1
        return f"release-snapshot-{token_index}"

    def release(value: object) -> None:
        nonlocal released_records
        if not isinstance(value, UIElement):
            raise RuntimeError("snapshot release received an unexpected record")
        released_records += 1

    store = ObservationStore(
        max_snapshots=capacity,
        token_factory=token_factory,
    )
    max_live_snapshots = 0
    for revision in range(cycles):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=f"deep-target-{revision}",
            generation=0,
            revision=revision,
            elements=_deep_tree_records(
                tree_depth,
                payload_bytes,
                payload_references,
            ),
            release=release,
        )
        max_live_snapshots = max(max_live_snapshots, len(store._snapshots))

    gc.collect()
    live_payloads_before_close = sum(
        reference() is not None for reference in payload_references
    )
    retained_payload_bytes_proxy = live_payloads_before_close * payload_bytes
    proxy_gate_bytes = capacity * tree_depth * payload_bytes
    if retained_payload_bytes_proxy > proxy_gate_bytes:
        raise RuntimeError(
            "snapshot retention exceeded the deterministic RSS proxy gate"
        )

    store.close()
    gc.collect()
    live_payloads_after_close = sum(
        reference() is not None for reference in payload_references
    )
    leaked_snapshots = len(store._snapshots)
    if live_payloads_after_close or leaked_snapshots:
        raise RuntimeError("snapshot close retained deep provider state")

    return {
        "cycles": cycles,
        "tree_depth": tree_depth,
        "capacity": capacity,
        "payload_bytes": payload_bytes,
        "max_live_snapshots": max_live_snapshots,
        "live_payloads_before_close": live_payloads_before_close,
        "live_payloads_after_close": live_payloads_after_close,
        "retained_payload_bytes_proxy": retained_payload_bytes_proxy,
        "proxy_gate_bytes": proxy_gate_bytes,
        "released_records": released_records,
        "leaked_snapshots": leaked_snapshots,
    }


class _BrowserFixtureAdapter:
    def __init__(self, expected_window: UIElement) -> None:
        self.expected_window = expected_window
        self.scoped_reads = 0
        self.full_reads = 0

    def get_tree(self, _pid: int, max_depth: int = 10) -> UIElement:
        self.full_reads += 1
        raise AssertionError(f"browser fixture used a full tree at depth {max_depth}")

    def get_subtree(self, element: UIElement, max_depth: int = 10) -> UIElement:
        if element is not self.expected_window or max_depth != 10:
            raise AssertionError("browser fixture refreshed the wrong native window")
        self.scoped_reads += 1
        return UIElement(
            id=10,
            role="document",
            name="Bitbucket pull request",
            platform_ref=object(),
            children=[
                UIElement(
                    id=11,
                    role="heading",
                    name="Pull request 482",
                    platform_ref=object(),
                )
            ],
        )


def _browser_targets() -> tuple[list[BrowserTarget], BrowserTarget]:
    chrome_window = UIElement(
        id=1,
        role="window",
        name="Google Chrome",
        platform_ref=object(),
    )
    firefox_window = UIElement(
        id=2,
        role="window",
        name="Firefox",
        platform_ref=object(),
    )
    safari_window = UIElement(
        id=3,
        role="window",
        name="Safari",
        platform_ref=object(),
    )
    targets = [
        BrowserTarget(
            browser="Google Chrome",
            pid=100,
            title="Inbox",
            url="https://mail.example.test",
            window_index=0,
            tab_index=1,
            element=UIElement(id=21, role="tab", name="Inbox"),
            window_element=chrome_window,
        ),
        BrowserTarget(
            browser="Firefox",
            pid=200,
            title="BIT-482 pagination guard pull request",
            url="https://bitbucket.example.test/acme/repo/pull-requests/482",
            window_index=1,
            tab_index=2,
            element=UIElement(id=22, role="tab", name="BIT-482 pull request"),
            window_element=firefox_window,
        ),
        BrowserTarget(
            browser="Safari",
            pid=300,
            title="Project board",
            url="https://jira.example.test/browse/BIT-482",
            window_index=0,
            tab_index=0,
            element=UIElement(id=23, role="tab", name="Project board"),
            window_element=safari_window,
        ),
    ]
    return targets, targets[1]


async def _browser_target_evidence() -> dict[str, int | str]:
    """Prove a browser query inventories all fixtures and activates one exact target."""
    from agent_eyes import server

    targets, expected = _browser_targets()
    adapter = _BrowserFixtureAdapter(expected.window_element)
    coordinator = AutomationCoordinator()
    inline_worker = _InlineWorker()
    inventory_calls = 0
    activation_calls = 0
    shadow_probes = 0
    activated_target: BrowserTarget | None = None

    def inventory(_adapter, *, require_complete: bool = False):
        nonlocal inventory_calls
        if require_complete:
            raise AssertionError(
                "existing-target reuse must isolate unrelated browser failures"
            )
        inventory_calls += 1
        return targets

    async def activate(target, timeout: float = 0.75, *, budget=None):
        nonlocal activation_calls, activated_target
        if timeout <= 0 or budget is not None:
            raise AssertionError(
                "transaction activation fixture received an invalid call"
            )
        activation_calls += 1
        activated_target = target
        return True

    async def shadow_inventory():
        nonlocal shadow_probes
        shadow_probes += 1
        return [], "none"

    saved = {
        "collect_browser_targets": server.collect_browser_targets,
        "activate": server._activate_browser_target_and_wait,
        "shadow_inventory": server._collect_explicit_shadow_tabs,
        "native_adapter": server.native_adapter,
        "native_worker": server.native_worker,
        "input_worker": server.input_worker,
        "runtime_readiness": server._runtime_readiness,
        "resolver": server._transaction_target_resolver,
        "coordinator": server.coordinator,
        "telemetry": server._transaction_telemetry,
        "dispatch": server._DISPATCH_TABLE,
    }
    server.collect_browser_targets = inventory
    server._activate_browser_target_and_wait = activate
    server._collect_explicit_shadow_tabs = shadow_inventory
    server.native_adapter = adapter
    server.native_worker = inline_worker
    server.input_worker = inline_worker
    server._runtime_readiness = SimpleNamespace(core_ready=True)
    server._transaction_target_resolver = None
    server.coordinator = coordinator
    server._transaction_telemetry = None
    server._DISPATCH_TABLE = None

    try:
        payload = _payload(
            await server.call_tool(
                "execute",
                {
                    "target": {
                        "query": "BIT-482 pagination guard Bitbucket pull request"
                    },
                    "steps": [
                        {
                            "op": "expect",
                            "role": "heading",
                            "name": "Pull request 482",
                        }
                    ],
                    "deadline_ms": 3_000,
                },
            )
        )
        return {
            "inventory_calls": inventory_calls,
            "inventory_targets": len(targets),
            "activation_calls": activation_calls,
            "activated_target_id": (
                activated_target.identifier if activated_target is not None else ""
            ),
            "expected_target_id": expected.identifier,
            "activated_browser": (
                activated_target.browser if activated_target is not None else ""
            ),
            "scoped_reads": adapter.scoped_reads,
            "full_reads": adapter.full_reads,
            "completed_steps": int(payload["completed_steps"]),
            "status": str(payload["status"]),
            "implicit_shadow_probes": shadow_probes,
        }
    finally:
        await coordinator.close()
        server.collect_browser_targets = saved["collect_browser_targets"]
        server._activate_browser_target_and_wait = saved["activate"]
        server._collect_explicit_shadow_tabs = saved["shadow_inventory"]
        server.native_adapter = saved["native_adapter"]
        server.native_worker = saved["native_worker"]
        server.input_worker = saved["input_worker"]
        server._runtime_readiness = saved["runtime_readiness"]
        server._transaction_target_resolver = saved["resolver"]
        server.coordinator = saved["coordinator"]
        server._transaction_telemetry = saved["telemetry"]
        server._DISPATCH_TABLE = saved["dispatch"]


async def _benchmark(
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    cancellation_count: int = _DEFAULT_CANCELLATIONS,
    snapshot_cycles: int = _DEFAULT_SNAPSHOT_CYCLES,
    tree_depth: int = _DEFAULT_TREE_DEPTH,
) -> dict[str, object]:
    queue = await _foreground_queue_evidence(
        concurrency=concurrency,
        cancellation_count=cancellation_count,
    )
    snapshots = _snapshot_retention_evidence(
        cycles=snapshot_cycles,
        tree_depth=tree_depth,
        capacity=_DEFAULT_SNAPSHOT_CAPACITY,
        payload_bytes=_DEFAULT_PAYLOAD_BYTES,
    )
    browser = await _browser_target_evidence()
    expected_completions = concurrency - cancellation_count
    expected_retained_payloads = int(snapshots["capacity"]) * tree_depth
    expected_releases = snapshot_cycles * tree_depth
    passed = bool(
        queue["entered_queue"] == concurrency
        and queue["cancelled"] == cancellation_count
        and queue["completed"] == expected_completions
        and queue["started_operations"] == expected_completions
        and queue["completed_operations"] == expected_completions
        and queue["max_active_operations"] == 1
        and queue["tree_reads"] == expected_completions
        and queue["dispatches"] == expected_completions
        and queue["cancelled_dispatches"] == 0
        and queue["duplicate_dispatches"] == 0
        and queue["queue_locked_after"] is False
        and snapshots["max_live_snapshots"] == snapshots["capacity"]
        and snapshots["live_payloads_before_close"] == expected_retained_payloads
        and snapshots["retained_payload_bytes_proxy"] == snapshots["proxy_gate_bytes"]
        and snapshots["live_payloads_after_close"] == 0
        and snapshots["released_records"] == expected_releases
        and snapshots["leaked_snapshots"] == 0
        and browser["inventory_calls"] == 1
        and browser["inventory_targets"] == 3
        and browser["activation_calls"] == 1
        and browser["activated_target_id"] == browser["expected_target_id"]
        and browser["scoped_reads"] == 1
        and browser["full_reads"] == 0
        and browser["completed_steps"] == 1
        and browser["status"] == "succeeded"
        and browser["implicit_shadow_probes"] == 0
    )
    return {
        "schema_version": 1,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "agent_eyes": __version__,
        },
        "protocol": {
            "fixture": "deterministic public execute and in-process retention evidence",
            "network_calls": 0,
            "live_ui_calls": 0,
            "fixed_sleeps": 0,
        },
        "foreground_queue": queue,
        "snapshot_retention": snapshots,
        "browser_target": browser,
        "gates": {"passed": passed},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--concurrency",
        type=_positive_integer,
        default=_DEFAULT_CONCURRENCY,
    )
    parser.add_argument(
        "--cancellations",
        type=_positive_integer,
        default=_DEFAULT_CANCELLATIONS,
    )
    parser.add_argument(
        "--snapshot-cycles",
        type=_positive_integer,
        default=_DEFAULT_SNAPSHOT_CYCLES,
    )
    parser.add_argument(
        "--tree-depth",
        type=_positive_integer,
        default=_DEFAULT_TREE_DEPTH,
    )
    arguments = parser.parse_args()
    result = asyncio.run(
        _benchmark(
            concurrency=arguments.concurrency,
            cancellation_count=arguments.cancellations,
            snapshot_cycles=arguments.snapshot_cycles,
            tree_depth=arguments.tree_depth,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["gates"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
