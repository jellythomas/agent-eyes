"""Seeded concurrency, target-isolation, and CDP lifecycle stress checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

from agent_eyes.cdp_persistent import CDPConnection
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationError, OperationErrorCode, OperationMode


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


async def _singleflight_schedules(schedules: int, seed: int) -> dict[str, int]:
    generator = random.Random(seed)
    provider_calls = 0
    cancelled_waiters = 0
    for schedule in range(schedules):
        coordinator = AutomationCoordinator()
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0
        concurrency = generator.randint(2, 16)
        key = ("stress", schedule)

        async def producer() -> object:
            nonlocal calls, provider_calls
            calls += 1
            provider_calls += 1
            started.set()
            await release.wait()
            return object()

        tasks = [
            asyncio.create_task(coordinator.observe(key, producer))
            for _ in range(concurrency)
        ]
        await started.wait()
        while coordinator._flights[key].waiters < concurrency:
            await asyncio.sleep(0)

        cancellation_count = generator.randint(0, concurrency - 1)
        cancelled = set(generator.sample(range(concurrency), cancellation_count))
        for index in cancelled:
            tasks[index].cancel()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        survivors = [
            value
            for index, value in enumerate(results)
            if index not in cancelled
        ]
        if calls != 1 or not survivors or not all(value is survivors[0] for value in survivors):
            raise RuntimeError(f"single-flight invariant failed at schedule {schedule}")
        if coordinator._flights:
            await asyncio.sleep(0)
        if coordinator._flights:
            raise RuntimeError(f"flight leaked at schedule {schedule}")
        cancelled_waiters += cancellation_count
        await coordinator.close()
    return {
        "schedules": schedules,
        "provider_calls": provider_calls,
        "cancelled_waiters": cancelled_waiters,
        "leaked_flights": 0,
        "wrong_results": 0,
    }


def _target_isolation_schedules(schedules: int) -> dict[str, int]:
    token_index = 0

    def token_factory() -> str:
        nonlocal token_index
        token_index += 1
        return f"stress-{token_index}"

    store = ObservationStore(
        max_snapshots=2,
        max_elements_per_snapshot=2,
        ttl_seconds=60,
        token_factory=token_factory,
    )
    rejected_cross_target_resolutions = 0
    for schedule in range(schedules):
        target_a = f"target-a-{schedule}"
        target_b = f"target-b-{schedule}"
        snapshot_a = store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=target_a,
            generation=0,
            revision=schedule,
            elements=[ElementRecord(1, (target_a, 1))],
        )
        snapshot_b = store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=target_b,
            generation=0,
            revision=schedule,
            elements=[ElementRecord(1, (target_b, 1))],
        )
        if store.resolve(snapshot_a.token, 1, expected_target_id=target_a).value != (
            target_a,
            1,
        ):
            raise RuntimeError(f"wrong target A result at schedule {schedule}")
        if store.resolve(snapshot_b.token, 1, expected_target_id=target_b).value != (
            target_b,
            1,
        ):
            raise RuntimeError(f"wrong target B result at schedule {schedule}")
        try:
            store.resolve(snapshot_a.token, 1, expected_target_id=target_b)
        except OperationError as exc:
            if exc.code is not OperationErrorCode.TARGET_MISMATCH:
                raise
            rejected_cross_target_resolutions += 1
        else:
            raise RuntimeError(f"cross-target resolution succeeded at schedule {schedule}")
    store.close()
    return {
        "schedules": schedules,
        "rejected_cross_target_resolutions": rejected_cross_target_resolutions,
        "wrong_target_resolutions": 0,
    }


def _cdp_attach_detach_cycles(cycles: int) -> dict[str, int]:
    connection = CDPConnection()
    for cycle in range(cycles):
        target_id = f"target-{cycle}"
        session_id = f"session-{cycle}"
        connection._on_attached(
            {
                "sessionId": session_id,
                "targetInfo": {
                    "targetId": target_id,
                    "type": "page",
                    "title": target_id,
                    "url": f"https://example.test/{cycle}",
                    "webSocketDebuggerUrl": (
                        f"ws://127.0.0.1:9222/devtools/page/{target_id}"
                    ),
                },
            }
        )
        connection._on_detached(
            {"sessionId": session_id, "targetId": target_id}
        )
    leaked_records = (
        len(connection._sessions)
        + len(connection._tabs)
        + len(connection._tab_by_target)
    )
    if leaked_records:
        raise RuntimeError(f"CDP lifecycle leaked {leaked_records} records")
    return {"cycles": cycles, "leaked_records": leaked_records}


async def _run(schedules: int, attach_cycles: int, seed: int) -> dict[str, object]:
    started = time.perf_counter()
    singleflight = await _singleflight_schedules(schedules, seed)
    target_isolation = _target_isolation_schedules(schedules)
    cdp_lifecycle = _cdp_attach_detach_cycles(attach_cycles)
    return {
        "schema_version": 1,
        "seed": seed,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "singleflight": singleflight,
        "target_isolation": target_isolation,
        "cdp_lifecycle": cdp_lifecycle,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedules", type=_positive_integer, default=1_000)
    parser.add_argument("--attach-cycles", type=_positive_integer, default=10_000)
    parser.add_argument("--seed", type=int, default=20_260_718)
    arguments = parser.parse_args()
    result = asyncio.run(
        _run(arguments.schedules, arguments.attach_cycles, arguments.seed)
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
