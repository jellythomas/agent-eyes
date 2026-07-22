"""Seeded concurrency, setup-lock, snapshot-memory, and CDP stress checks."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import multiprocessing
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import tempfile
import time

from agent_eyes.cdp_persistent import CDPConnection
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationError, OperationErrorCode, OperationMode
from agent_eyes.setup.state import setup_process_lock


_SETUP_LOCK_WORKERS = 4
_SETUP_LOCK_ROUNDS = 25
_SETUP_LOCK_TIMEOUT_SECONDS = 30.0


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _windows_current_rss_bytes() -> int:
    """Read the process working set through the Windows process-status API."""
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    ):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "GetProcessMemoryInfo failed")
    return int(counters.working_set_size)


def _current_rss_bytes() -> tuple[int, str]:
    """Return current resident memory without adding a benchmark dependency."""
    if sys.platform.startswith("linux") and os.path.exists("/proc/self/statm"):
        with open("/proc/self/statm", encoding="ascii") as statm:
            fields = statm.read().split()
        if len(fields) < 2:
            raise RuntimeError("/proc/self/statm did not contain resident pages")
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return int(fields[1]) * page_size, "proc-self-statm"

    if sys.platform == "win32":
        return _windows_current_rss_bytes(), "windows-working-set"

    ps_path = next(
        (path for path in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(path)),
        None,
    )
    if ps_path is None:
        raise RuntimeError("current RSS is unavailable: ps executable not found")
    completed = subprocess.run(
        [ps_path, "-o", "rss=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
        text=True,
    )
    rss_kib = int(completed.stdout.strip())
    if rss_kib < 1:
        raise RuntimeError("ps reported a non-positive resident set size")
    return rss_kib * 1024, "ps-rss"


class _SnapshotPayload:
    __slots__ = ("buffer",)

    def __init__(self, size: int) -> None:
        self.buffer = bytearray(size)
        self.buffer[0] = 1  # Commit the page so retained payloads affect RSS.


def _snapshot_rss_cycles(cycles: int) -> dict[str, int | float | str]:
    """Exercise snapshot ownership and enforce the release RSS growth gate."""
    warmup_cycles = min(512, cycles)
    payload_bytes = 4 * 1024
    minimum_threshold_bytes = 10 * 1024 * 1024
    token_index = 0
    released_records = 0

    def token_factory() -> str:
        nonlocal token_index
        token_index += 1
        return f"snapshot-stress-{token_index}"

    def release(value: object) -> None:
        nonlocal released_records
        if not isinstance(value, _SnapshotPayload):
            raise RuntimeError("snapshot stress released an unexpected record")
        released_records += 1

    store = ObservationStore(max_snapshots=4, token_factory=token_factory)

    def exercise(revision: int) -> int:
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="snapshot-memory-target",
            generation=1,
            revision=revision,
            elements=[ElementRecord(1, _SnapshotPayload(payload_bytes))],
            release=release,
        )
        live_snapshots = len(store._snapshots)
        invalidated = store.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="snapshot-memory-target",
        )
        if invalidated != 1 or store._snapshots:
            raise RuntimeError(
                f"snapshot cycle {revision} retained {len(store._snapshots)} records"
            )
        return live_snapshots

    for revision in range(warmup_cycles):
        exercise(revision)
    if released_records != warmup_cycles:
        raise RuntimeError("snapshot warmup did not release every provider record")

    gc.collect()
    _current_rss_bytes()  # Warm the measurement path before taking the baseline.
    gc.collect()
    baseline_rss_bytes, rss_source = _current_rss_bytes()
    released_before_measurement = released_records
    maximum_live_snapshots = 0

    for revision in range(cycles):
        maximum_live_snapshots = max(
            maximum_live_snapshots,
            exercise(warmup_cycles + revision),
        )

    gc.collect()
    final_rss_bytes, final_rss_source = _current_rss_bytes()
    if final_rss_source != rss_source:
        raise RuntimeError("RSS measurement source changed during snapshot stress")

    measured_releases = released_records - released_before_measurement
    leaked_snapshots = len(store._snapshots)
    store.close()
    if measured_releases != cycles:
        raise RuntimeError(
            f"snapshot stress released {measured_releases} of {cycles} records"
        )
    if maximum_live_snapshots > 1 or leaked_snapshots:
        raise RuntimeError(
            "snapshot store exceeded its one-record create/invalidate bound"
        )

    growth_bytes = final_rss_bytes - baseline_rss_bytes
    five_percent_bytes = (baseline_rss_bytes + 19) // 20
    threshold_bytes = max(minimum_threshold_bytes, five_percent_bytes)
    if growth_bytes > threshold_bytes:
        raise RuntimeError(
            "snapshot RSS growth exceeded the release gate: "
            f"{growth_bytes} > {threshold_bytes} bytes"
        )

    growth_percent = (
        round((growth_bytes / baseline_rss_bytes) * 100, 3)
        if baseline_rss_bytes
        else 0.0
    )
    return {
        "cycles": cycles,
        "warmup_cycles": warmup_cycles,
        "payload_bytes_per_cycle": payload_bytes,
        "rss_source": rss_source,
        "baseline_rss_bytes": baseline_rss_bytes,
        "final_rss_bytes": final_rss_bytes,
        "growth_bytes": growth_bytes,
        "growth_percent": growth_percent,
        "threshold_bytes": threshold_bytes,
        "minimum_threshold_bytes": minimum_threshold_bytes,
        "threshold_percent": 5,
        "released_records": measured_releases,
        "maximum_live_snapshots": maximum_live_snapshots,
        "leaked_snapshots": leaked_snapshots,
    }


def _setup_lock_worker(
    lock_path: str,
    counter_path: str,
    barrier,
    result_queue,
) -> None:
    """Contend for the setup lock from a spawned interpreter."""
    completed = 0
    try:
        selected_lock = Path(lock_path)
        selected_counter = Path(counter_path)
        for _ in range(_SETUP_LOCK_ROUNDS):
            barrier.wait(timeout=_SETUP_LOCK_TIMEOUT_SECONDS)
            with setup_process_lock(selected_lock):
                counter = int(selected_counter.read_text(encoding="ascii"))
                selected_counter.write_text(str(counter + 1), encoding="ascii")
            completed += 1
        result_queue.put((os.getpid(), completed, None))
    except Exception as exc:  # pragma: no cover - reported by the parent process
        result_queue.put((os.getpid(), completed, f"{type(exc).__name__}: {exc}"))


def _setup_process_lock_schedules() -> dict[str, int]:
    """Prove cross-process setup-lock exclusion under synchronized contention."""
    context = multiprocessing.get_context("spawn")
    acquisitions = _SETUP_LOCK_WORKERS * _SETUP_LOCK_ROUNDS
    with tempfile.TemporaryDirectory(prefix="agent-eyes-lock-stress-") as temp_dir:
        root = Path(temp_dir)
        lock_path = root / ".setup.lock"
        counter_path = root / "counter"
        counter_path.write_text("0", encoding="ascii")
        barrier = context.Barrier(_SETUP_LOCK_WORKERS)
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_setup_lock_worker,
                args=(str(lock_path), str(counter_path), barrier, result_queue),
            )
            for _ in range(_SETUP_LOCK_WORKERS)
        ]
        started_processes = []
        deadline = time.monotonic() + _SETUP_LOCK_TIMEOUT_SECONDS
        results: list[tuple[int, int, str | None]] = []
        try:
            for process in processes:
                process.start()
                started_processes.append(process)

            for _ in processes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "setup-lock workers exceeded the stress deadline"
                    )
                try:
                    results.append(result_queue.get(timeout=remaining))
                except queue.Empty as exc:
                    raise RuntimeError(
                        "setup-lock workers exceeded the stress deadline"
                    ) from exc

            for process in processes:
                process.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(process.is_alive() for process in processes):
                raise RuntimeError(
                    "setup-lock workers did not exit before the deadline"
                )
            exit_codes = [process.exitcode for process in processes]
            if any(exit_code != 0 for exit_code in exit_codes):
                raise RuntimeError(
                    f"setup-lock workers exited unsuccessfully: {exit_codes}"
                )

            child_errors = [error for _, _, error in results if error is not None]
            if child_errors:
                raise RuntimeError(f"setup-lock workers failed: {child_errors}")
            completed_acquisitions = sum(completed for _, completed, _ in results)
            final_counter = int(counter_path.read_text(encoding="ascii"))
            if completed_acquisitions != acquisitions or final_counter != acquisitions:
                raise RuntimeError(
                    "setup-lock mutual exclusion failed: "
                    f"completed={completed_acquisitions}, counter={final_counter}, "
                    f"expected={acquisitions}"
                )
        finally:
            barrier.abort()
            for process in started_processes:
                if process.is_alive():
                    process.terminate()
            terminate_deadline = time.monotonic() + 5.0
            for process in started_processes:
                process.join(timeout=max(0.0, terminate_deadline - time.monotonic()))
            for process in started_processes:
                if process.is_alive():
                    process.kill()
            kill_deadline = time.monotonic() + 5.0
            for process in started_processes:
                process.join(timeout=max(0.0, kill_deadline - time.monotonic()))
                if not process.is_alive():
                    process.close()
            result_queue.close()
            result_queue.join_thread()

    return {
        "workers": _SETUP_LOCK_WORKERS,
        "rounds_per_worker": _SETUP_LOCK_ROUNDS,
        "acquisitions": acquisitions,
        "completed_acquisitions": completed_acquisitions,
        "final_counter": final_counter,
        "child_errors": 0,
    }


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
            value for index, value in enumerate(results) if index not in cancelled
        ]
        if (
            calls != 1
            or not survivors
            or not all(value is survivors[0] for value in survivors)
        ):
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
            raise RuntimeError(
                f"cross-target resolution succeeded at schedule {schedule}"
            )
    store.close()
    return {
        "schedules": schedules,
        "rejected_cross_target_resolutions": rejected_cross_target_resolutions,
        "wrong_target_resolutions": 0,
    }


def _attach_cdp_target(
    connection: CDPConnection,
    target_id: str,
    session_id: str,
) -> None:
    connection._on_attached(
        {
            "sessionId": session_id,
            "targetInfo": {
                "targetId": target_id,
                "type": "page",
                "title": target_id,
                "url": f"https://example.test/{target_id}",
                "webSocketDebuggerUrl": (
                    f"ws://127.0.0.1:9222/devtools/page/{target_id}"
                ),
            },
        }
    )


def _cdp_attach_detach_cycles(cycles: int) -> dict[str, int]:
    connection = CDPConnection()
    for cycle in range(cycles):
        target_id = f"target-{cycle}"
        session_id = f"session-{cycle}"
        _attach_cdp_target(connection, target_id, session_id)
        connection._on_detached({"sessionId": session_id, "targetId": target_id})
    leaked_records = (
        len(connection._sessions)
        + len(connection._tabs)
        + len(connection._tab_by_target)
    )
    if leaked_records:
        raise RuntimeError(f"CDP lifecycle leaked {leaked_records} records")
    return {"cycles": cycles, "leaked_records": leaked_records}


async def _cdp_reconnect_cycles(cycles: int) -> dict[str, int]:
    class ProbeWebSocket:
        async def close(self) -> None:
            return None

    connection = CDPConnection()
    connection._generation = 1
    connection._connected = True
    connection._ws = ProbeWebSocket()
    _attach_cdp_target(connection, "target-live", "session-live-1")

    async def install_replacement() -> None:
        connection._generation += 1
        connection._connected = True
        connection._ws = ProbeWebSocket()
        _attach_cdp_target(
            connection,
            "target-live",
            f"session-live-{connection._generation}",
        )

    connection._ensure_connected_locked = install_replacement
    maximum_records = 0
    for cycle in range(cycles):
        stale_target_id = f"target-closed-{cycle}"
        _attach_cdp_target(
            connection,
            stale_target_id,
            f"session-closed-{cycle}",
        )

        await connection.reconnect(connection._generation)

        current = connection.get_session_for_target("target-live")
        if current is None or current.generation != connection._generation:
            raise RuntimeError(f"CDP reconnect lost current target at cycle {cycle}")
        if connection.get_session_for_target(stale_target_id) is not None:
            raise RuntimeError(f"CDP reconnect retained stale target at cycle {cycle}")
        records = (
            len(connection._sessions)
            + len(connection._tabs)
            + len(connection._tab_by_target)
        )
        maximum_records = max(maximum_records, records)
        if records != 3:
            raise RuntimeError(
                f"CDP reconnect retained {records - 3} stale records at cycle {cycle}"
            )

    return {
        "cycles": cycles,
        "leaked_records": 0,
        "maximum_records": maximum_records,
        "wrong_generation_bindings": 0,
    }


async def _run(
    schedules: int,
    snapshot_cycles: int,
    attach_cycles: int,
    reconnect_cycles: int,
    seed: int,
) -> dict[str, object]:
    started = time.perf_counter()
    singleflight = await _singleflight_schedules(schedules, seed)
    target_isolation = _target_isolation_schedules(schedules)
    snapshot_memory = _snapshot_rss_cycles(snapshot_cycles)
    setup_process_lock_stress = _setup_process_lock_schedules()
    cdp_lifecycle = _cdp_attach_detach_cycles(attach_cycles)
    cdp_reconnect = await _cdp_reconnect_cycles(reconnect_cycles)
    return {
        "schema_version": 4,
        "seed": seed,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "singleflight": singleflight,
        "target_isolation": target_isolation,
        "snapshot_memory": snapshot_memory,
        "setup_process_lock": setup_process_lock_stress,
        "cdp_lifecycle": cdp_lifecycle,
        "cdp_reconnect": cdp_reconnect,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedules", type=_positive_integer, default=1_000)
    parser.add_argument("--snapshot-cycles", type=_positive_integer, default=10_000)
    parser.add_argument("--attach-cycles", type=_positive_integer, default=10_000)
    parser.add_argument("--reconnect-cycles", type=_positive_integer, default=1_000)
    parser.add_argument("--seed", type=int, default=20_260_718)
    arguments = parser.parse_args()
    result = asyncio.run(
        _run(
            arguments.schedules,
            arguments.snapshot_cycles,
            arguments.attach_cycles,
            arguments.reconnect_cycles,
            arguments.seed,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
