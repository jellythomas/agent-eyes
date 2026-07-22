"""Deterministic runtime, concurrency, event, and token benchmarks for Agent Eyes."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_eyes import __version__
from agent_eyes.browser_inventory import BrowserTarget, format_browser_targets
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.input_sim import InputBackend
from agent_eyes.native_events import run_native_action_until
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationBudget, OperationError, OperationMode
from agent_eyes.provider_worker import ProviderWorker
from agent_eyes.server import TOOLS


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _PROJECT_ROOT / "src" / "agent_eyes"
_ALLOWED_SLEEP_MODULES = frozenset({
    Path("input_sim.py"),
    Path("native_events.py"),
    Path("setup/readiness.py"),
})


def _sleep_calls_in(source_paths: tuple[Path, ...]) -> list[str]:
    """Return imported time/asyncio sleep call locations in Python sources."""
    locations: list[str] = []
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        module_aliases: set[str] = set()
        direct_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name in {"asyncio", "time"}:
                        module_aliases.add(imported.asname or imported.name)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module in {"asyncio", "time"}
            ):
                for imported in node.names:
                    if imported.name == "sleep":
                        direct_aliases.add(imported.asname or imported.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_module_sleep = (
                isinstance(function, ast.Attribute)
                and function.attr == "sleep"
                and isinstance(function.value, ast.Name)
                and function.value.id in module_aliases
            )
            is_direct_sleep = (
                isinstance(function, ast.Name) and function.id in direct_aliases
            )
            if is_module_sleep or is_direct_sleep:
                locations.append(f"{source_path}:{node.lineno}")
    return sorted(locations)


def _fixed_orchestration_sleep_calls() -> list[str]:
    """Find sleeps outside explicit input, adaptive-wait, and setup polling paths."""
    source_paths = tuple(
        source_path
        for source_path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if source_path.relative_to(_SOURCE_ROOT) not in _ALLOWED_SLEEP_MODULES
    )
    return _sleep_calls_in(source_paths)


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def _git_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _subprocess_sample(arguments: list[str]) -> float:
    started = time.perf_counter()
    subprocess.run(
        arguments,
        cwd=_PROJECT_ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    return (time.perf_counter() - started) * 1_000


def _measure_sync(
    call: Callable[[], Any],
    *,
    samples: int,
    warmups: int,
) -> tuple[dict[str, float], Any]:
    latencies: list[float] = []
    value: Any = None
    for index in range(samples + warmups):
        started = time.perf_counter()
        value = call()
        elapsed = (time.perf_counter() - started) * 1_000
        if index >= warmups:
            latencies.append(elapsed)
    return _summary(latencies), value


async def _measure_async(
    call: Callable[[], Awaitable[Any]],
    *,
    samples: int,
    warmups: int,
) -> tuple[dict[str, float], Any]:
    latencies: list[float] = []
    value: Any = None
    for index in range(samples + warmups):
        started = time.perf_counter()
        value = await call()
        elapsed = (time.perf_counter() - started) * 1_000
        if index >= warmups:
            latencies.append(elapsed)
    return _summary(latencies), value


class _ImmediateSubscription:
    generation = 0

    async def wait_for_change(self, after_generation: int, timeout: float) -> bool:
        return self.generation > after_generation

    async def aclose(self) -> None:
        return None


class _NoopInputBackend(InputBackend):
    def is_available(self) -> bool:
        return True

    def type_text(
        self,
        text: str,
        delay: float = 0.02,
        human_like: bool = False,
    ) -> bool:
        return bool(text) and not human_like

    def press_key(self, key: str) -> bool:
        return bool(key)

    def hotkey(self, *keys: str) -> bool:
        return bool(keys)

    def click(self, x: int, y: int, button: str = "left") -> bool:
        return button == "left"

    def scroll(
        self,
        x: int,
        y: int,
        delta_x: int = 0,
        delta_y: int = -3,
    ) -> bool:
        return True

    def activate_window(self, pid: int) -> bool:
        return pid > 0


def _targets(count: int) -> list[BrowserTarget]:
    return [
        BrowserTarget(
            browser="Firefox" if index % 2 else "Safari",
            pid=1_000 + (index % 4),
            title=f"Benchmark target {index}",
            url=f"https://example.test/items/{index}?secret=redacted",
            window_index=index // 20,
            tab_index=index,
            selected=index == 0,
            frontmost=index == 0,
        )
        for index in range(count)
    ]


async def _singleflight_sample(concurrency: int) -> tuple[float, int]:
    coordinator = AutomationCoordinator()
    release = asyncio.Event()
    started = asyncio.Event()
    provider_calls = 0

    async def producer() -> object:
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        await release.wait()
        return object()

    before = time.perf_counter()
    tasks = [
        asyncio.create_task(coordinator.observe("shared", producer))
        for _ in range(concurrency)
    ]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    elapsed = (time.perf_counter() - before) * 1_000
    if provider_calls != 1 or not all(value is results[0] for value in results):
        raise RuntimeError("single-flight correctness invariant failed")
    await coordinator.close()
    return elapsed, provider_calls


async def _heartbeat_sample() -> float:
    worker = ProviderWorker("benchmark-heartbeat")
    try:
        provider = asyncio.create_task(
            worker.run(
                lambda: time.sleep(0.05),
                budget=OperationBudget.start(1.0),
                operation="benchmark provider",
            )
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.sleep(0.01)
        lag = max(0.0, loop.time() - started - 0.01) * 1_000
        await provider
        return lag
    finally:
        await worker.aclose()


async def _deadline_sample() -> float:
    worker = ProviderWorker("benchmark-deadline")
    started = time.perf_counter()
    try:
        try:
            await worker.run(
                lambda: time.sleep(0.05),
                budget=OperationBudget.start(0.01),
                operation="benchmark deadline",
            )
        except OperationError:
            return (time.perf_counter() - started) * 1_000
        raise RuntimeError("slow provider unexpectedly completed inside its deadline")
    finally:
        await worker.wait_until_idle()
        await worker.aclose()


async def _benchmark(samples: int, warmups: int, concurrency: int) -> dict[str, object]:
    fixed_orchestration_sleep_calls = _fixed_orchestration_sleep_calls()
    if fixed_orchestration_sleep_calls:
        joined = ", ".join(fixed_orchestration_sleep_calls)
        raise RuntimeError(f"fixed orchestration sleep call found: {joined}")

    cli_import = _summary([
        _subprocess_sample([sys.executable, "-c", "import agent_eyes.cli"])
        for _ in range(samples + warmups)
    ][warmups:])
    cli_help = _summary([
        _subprocess_sample([sys.executable, "-m", "agent_eyes.cli", "--help"])
        for _ in range(samples + warmups)
    ][warmups:])

    store = ObservationStore(
        max_elements_per_snapshot=128,
        token_factory=lambda: "benchmark-token",
    )
    snapshot = store.create(
        provider="benchmark",
        mode=OperationMode.FOREGROUND,
        target_id="target",
        generation=1,
        revision=1,
        elements=[ElementRecord(index, object()) for index in range(100)],
    )
    snapshot_resolve, _ = _measure_sync(
        lambda: store.resolve(
            snapshot.token,
            99,
            expected_provider="benchmark",
            expected_mode=OperationMode.FOREGROUND,
            expected_target_id="target",
            expected_generation=1,
        ),
        samples=samples,
        warmups=warmups,
    )

    coordinator = AutomationCoordinator()

    async def foreground_noop() -> None:
        async def operation() -> None:
            return None

        await coordinator.execute_foreground(operation)

    coordinator_noop, _ = await _measure_async(
        foreground_noop,
        samples=samples,
        warmups=warmups,
    )
    await coordinator.close()

    async def immediate_event() -> None:
        result = await run_native_action_until(
            42,
            lambda: True,
            lambda: True,
            timeout=0.25,
            subscription_factory=lambda _pid: _ImmediateSubscription(),
        )
        if not result.condition_met or not result.event_driven or result.checks != 1:
            raise RuntimeError("immediate event benchmark did not use the event path")

    immediate_event_latency, _ = await _measure_async(
        immediate_event,
        samples=samples,
        warmups=warmups,
    )

    singleflight_latencies: list[float] = []
    singleflight_calls: set[int] = set()
    for index in range(samples + warmups):
        elapsed, provider_calls = await _singleflight_sample(concurrency)
        if index >= warmups:
            singleflight_latencies.append(elapsed)
            singleflight_calls.add(provider_calls)

    heartbeat_latencies: list[float] = []
    deadline_latencies: list[float] = []
    for index in range(samples + warmups):
        heartbeat = await _heartbeat_sample()
        deadline = await _deadline_sample()
        if index >= warmups:
            heartbeat_latencies.append(heartbeat)
            deadline_latencies.append(deadline)

    input_backend = _NoopInputBackend()
    fast_input, input_result = _measure_sync(
        lambda: input_backend.clear_and_type("x" * 1_000),
        samples=samples,
        warmups=warmups,
    )
    if input_result is not True:
        raise RuntimeError("fast input composite failed")

    format_results: dict[str, object] = {}
    for count in (50, 1_000):
        targets = _targets(count)
        latency, output = _measure_sync(
            lambda targets=targets: format_browser_targets(targets),
            samples=samples,
            warmups=warmups,
        )
        format_results[str(count)] = {
            "latency": latency,
            "output_bytes": len(output.encode("utf-8")),
            "shown_targets": output.count("[native:"),
        }

    catalog_json = json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in TOOLS],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return {
        "schema_version": 2,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "agent_eyes": __version__,
            "git_head": _git_head(),
        },
        "protocol": {
            "warmups": warmups,
            "samples": samples,
            "concurrency": concurrency,
            "percentile": "sorted[ceil(0.95*n)-1]",
            "fixture": "deterministic; no live browser or network latency",
        },
        "latency": {
            "cli_import": cli_import,
            "cli_help": cli_help,
            "snapshot_resolve": snapshot_resolve,
            "coordinator_foreground_noop": coordinator_noop,
            "immediate_event_completion": immediate_event_latency,
            "singleflight_observation": _summary(singleflight_latencies),
            "event_loop_heartbeat_lag": _summary(heartbeat_latencies),
            "provider_deadline_response": _summary(deadline_latencies),
            "fast_input_1000_char_composite": fast_input,
        },
        "correctness": {
            "singleflight_provider_calls": sorted(singleflight_calls),
            "fixed_orchestration_sleep_calls": len(
                fixed_orchestration_sleep_calls
            ),
            "allowed_sleep_modules": sorted(
                str(source_path) for source_path in _ALLOWED_SLEEP_MODULES
            ),
        },
        "formatting": format_results,
        "context": {
            "tool_count": len(TOOLS),
            "tools_list_compact_json_bytes": len(catalog_json),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=_positive_integer, default=30)
    parser.add_argument("--warmups", type=_non_negative_integer, default=3)
    parser.add_argument("--concurrency", type=_positive_integer, default=32)
    arguments = parser.parse_args()
    result = asyncio.run(
        _benchmark(arguments.samples, arguments.warmups, arguments.concurrency)
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
