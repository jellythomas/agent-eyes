"""Deterministic native-first MCP journey benchmarks for Agent Eyes."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mcp.types import CallToolResult

from agent_eyes import __version__
from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator


def _summary(samples: list[float], *, unit: str) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        f"median_{unit}": round(statistics.median(ordered), 3),
        f"p95_{unit}": round(ordered[p95_index], 3),
        f"max_{unit}": round(ordered[-1], 3),
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


class _InlineWorker:
    async def run(self, call, **_kwargs):
        return call()


@dataclass
class _Counters:
    inventory: int = 0
    activations: int = 0
    foreground_opens: int = 0
    shadow_probes: int = 0


def _text(result: Any) -> str:
    content = result.content if isinstance(result, CallToolResult) else result
    return "\n".join(item.text for item in content)


async def _run_journey(*, existing: bool) -> dict[str, object]:
    from agent_eyes import server

    target = BrowserTarget(
        browser="Firefox",
        pid=4_242,
        title="YouTube — No Na Rollerblade",
        url="https://www.youtube.com/results?search_query=no+na+rollerblade",
        window_index=0,
        tab_index=3,
        selected=False,
        frontmost=False,
        element=UIElement(id=3, role="tab", name="YouTube — No Na Rollerblade"),
        window_element=UIElement(id=1, role="window", name="Firefox"),
    )
    counters = _Counters()

    def inventory(_adapter):
        counters.inventory += 1
        return [target] if existing else []

    async def activate(_target, *, budget):
        counters.activations += 1
        return True

    def open_url(_url: str) -> tuple[bool, str]:
        counters.foreground_opens += 1
        return True, "opened"

    async def shadow_probe() -> bool:
        counters.shadow_probes += 1
        return True

    saved = {
        "collect_browser_targets": server.collect_browser_targets,
        "activate": server._activate_browser_target_and_wait,
        "open_url": server._pu.open_url_in_browser,
        "shadow_probe": server.cdp_client.is_available,
        "native_worker": server.native_worker,
        "coordinator": server.coordinator,
        "runtime_readiness": server._runtime_readiness,
        "native_adapter": server.native_adapter,
        "dispatch": server._DISPATCH_TABLE,
    }
    server.collect_browser_targets = inventory
    server._activate_browser_target_and_wait = activate
    server._pu.open_url_in_browser = open_url
    server.cdp_client.is_available = shadow_probe
    server.native_worker = _InlineWorker()
    server.coordinator = AutomationCoordinator()
    server._runtime_readiness = SimpleNamespace(core_ready=True)
    server.native_adapter = object()
    server._DISPATCH_TABLE = None

    arguments = {
        "url": "https://www.youtube.com/results?search_query=no+na+rollerblade",
        "query": "youtube no na rollerblade",
        "reuse_existing": True,
        "shadow": False,
    }
    started = time.perf_counter()
    try:
        listed = await server.call_tool(
            "list_tabs",
            {"query": arguments["query"], "max_results": 10, "shadow": False},
        )
        opened = await server.call_tool("new_tab", arguments)
        elapsed_ms = (time.perf_counter() - started) * 1_000
        outputs = [_text(listed), _text(opened)]
        errors = [
            result.isError
            for result in (listed, opened)
            if isinstance(result, CallToolResult)
        ]
        if any(errors):
            raise RuntimeError("journey returned an MCP error")
        if counters.shadow_probes:
            raise RuntimeError("foreground journey implicitly probed shadow mode")
        if existing and (counters.activations != 1 or counters.foreground_opens != 0):
            raise RuntimeError("existing-tab journey failed to reuse the matching tab")
        if not existing and (counters.activations != 0 or counters.foreground_opens != 1):
            raise RuntimeError("absent-tab journey did not open exactly one foreground tab")
        return {
            "elapsed_ms": elapsed_ms,
            "mcp_calls": 2,
            "output_bytes": sum(len(output.encode("utf-8")) for output in outputs),
            "inventory_calls": counters.inventory,
            "activation_calls": counters.activations,
            "foreground_open_calls": counters.foreground_opens,
            "implicit_shadow_probes": counters.shadow_probes,
        }
    finally:
        await server.coordinator.close()
        server.collect_browser_targets = saved["collect_browser_targets"]
        server._activate_browser_target_and_wait = saved["activate"]
        server._pu.open_url_in_browser = saved["open_url"]
        server.cdp_client.is_available = saved["shadow_probe"]
        server.native_worker = saved["native_worker"]
        server.coordinator = saved["coordinator"]
        server._runtime_readiness = saved["runtime_readiness"]
        server.native_adapter = saved["native_adapter"]
        server._DISPATCH_TABLE = saved["dispatch"]


async def _benchmark(samples: int, warmups: int) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for name, existing in (("existing_tab_reuse", True), ("new_tab_when_absent", False)):
        elapsed: list[float] = []
        output_bytes: list[float] = []
        evidence: dict[str, set[int]] = {
            "mcp_calls": set(),
            "inventory_calls": set(),
            "activation_calls": set(),
            "foreground_open_calls": set(),
            "implicit_shadow_probes": set(),
        }
        for index in range(samples + warmups):
            result = await _run_journey(existing=existing)
            if index >= warmups:
                elapsed.append(float(result["elapsed_ms"]))
                output_bytes.append(float(result["output_bytes"]))
                for key in evidence:
                    evidence[key].add(int(result[key]))
        scenarios[name] = {
            "latency": _summary(elapsed, unit="ms"),
            "output_size": _summary(output_bytes, unit="bytes"),
            "evidence": {key: sorted(value) for key, value in evidence.items()},
        }

    return {
        "schema_version": 1,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "agent_eyes": __version__,
        },
        "protocol": {
            "warmups": warmups,
            "samples": samples,
            "percentile": "sorted[ceil(0.95*n)-1]",
            "fixture": "deterministic native-first public call_tool path; no network latency",
        },
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=_positive_integer, default=30)
    parser.add_argument("--warmups", type=_non_negative_integer, default=3)
    arguments = parser.parse_args()
    result = asyncio.run(_benchmark(arguments.samples, arguments.warmups))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
