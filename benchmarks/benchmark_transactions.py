"""Deterministic transaction counts plus separately reported reference latency."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import time
from typing import Any

from agent_eyes import __version__

try:
    from benchmarks.transaction_fixture import (
        FIXTURE_SECRET,
        TransactionFixture,
        counter_delta,
        observe_arguments,
        post_arguments,
        result_payload,
        result_text,
        reveal_arguments,
    )
except ModuleNotFoundError:  # Direct execution from the benchmarks directory.
    from transaction_fixture import (  # type: ignore[no-redef]
        FIXTURE_SECRET,
        TransactionFixture,
        counter_delta,
        observe_arguments,
        post_arguments,
        result_payload,
        result_text,
        reveal_arguments,
    )


EXECUTE_OUTPUT_GATE_BYTES = 2 * 1024
OBSERVE_OUTPUT_GATE_BYTES = 4 * 1024
CATALOG_GATE_BYTES = 16 * 1024
REFERENCE_MEDIAN_TARGET_MS = 1_000.0
REFERENCE_P95_TARGET_MS = 3_000.0


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


def _record(evidence: dict[str, set[int]], values: dict[str, int]) -> None:
    for key, value in values.items():
        evidence.setdefault(key, set()).add(value)


async def _timed_call(
    fixture: TransactionFixture,
    name: str,
    arguments: dict[str, Any],
) -> tuple[Any, float, dict[str, int]]:
    before = fixture.counters.snapshot()
    started = time.perf_counter()
    result = await fixture.call_tool(name, arguments)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    return result, elapsed_ms, counter_delta(before, fixture.counters.snapshot())


async def _known_scenario(fixture: TransactionFixture) -> dict[str, Any]:
    fixture.reset("control_visible")
    result, elapsed_ms, delta = await _timed_call(fixture, "execute", post_arguments())
    payload = result_payload(result)
    rendered = result_text(result)
    if payload.get("status") != "succeeded" or fixture.phase != "posted":
        raise RuntimeError("known transaction fixture did not post the comment")
    if FIXTURE_SECRET in rendered:
        raise RuntimeError("typed fixture content leaked into execute output")
    return {
        "elapsed_ms": elapsed_ms,
        "output_bytes": len(rendered.encode("utf-8")),
        "max_activations_per_call": delta["activations"],
        "full_scans_before_first_mutation": fixture.first_mutation_full_scans,
        **delta,
    }


async def _discovery_scenario(fixture: TransactionFixture) -> dict[str, Any]:
    fixture.reset("control_visible", active=True)
    before = fixture.counters.snapshot()
    started = time.perf_counter()
    observed, _observe_ms, observe_delta = await _timed_call(
        fixture,
        "observe_target",
        observe_arguments(),
    )
    observed_payload = result_payload(observed)
    arguments = post_arguments()
    arguments["target"] = {
        "target_id": observed_payload["target"]["id"],
        "snapshot": observed_payload["snapshot"]["token"],
    }
    post, _post_ms, post_delta = await _timed_call(
        fixture,
        "execute",
        arguments,
    )
    elapsed_ms = (time.perf_counter() - started) * 1_000
    post_payload = result_payload(post)
    rendered = result_text(observed) + result_text(post)
    if (
        observed_payload.get("status") != "ok"
        or post_payload.get("status") != "succeeded"
        or fixture.phase != "posted"
    ):
        raise RuntimeError("discovery transaction fixture did not complete")
    if FIXTURE_SECRET in rendered:
        raise RuntimeError("typed fixture content leaked into discovery output")
    delta = counter_delta(before, fixture.counters.snapshot())
    return {
        "elapsed_ms": elapsed_ms,
        "output_bytes": len(result_text(post).encode("utf-8")),
        "observe_output_bytes": len(result_text(observed).encode("utf-8")),
        "max_activations_per_call": max(
            observe_delta["activations"],
            post_delta["activations"],
        ),
        "full_scans_before_first_mutation": fixture.first_mutation_full_scans,
        **delta,
    }


async def _reveal_scenario(fixture: TransactionFixture) -> dict[str, Any]:
    fixture.reset("virtualized")
    result, _elapsed_ms, delta = await _timed_call(
        fixture,
        "execute",
        reveal_arguments(),
    )
    payload = result_payload(result)
    rendered = result_text(result)
    if payload.get("status") != "succeeded" or fixture.phase != "control_visible":
        raise RuntimeError("reveal transaction fixture did not expose the control")
    return {
        "output_bytes": len(rendered.encode("utf-8")),
        "max_activations_per_call": delta["activations"],
        "full_scans_before_first_mutation": fixture.first_mutation_full_scans,
        **delta,
    }


async def _observe_budget_scenario(fixture: TransactionFixture) -> dict[str, Any]:
    fixture.reset("control_visible", active=True)
    result, _elapsed_ms, delta = await _timed_call(
        fixture,
        "observe_target",
        observe_arguments(),
    )
    payload = result_payload(result)
    rendered = result_text(result)
    if payload.get("status") != "ok":
        raise RuntimeError("observe_target fixture did not succeed")
    return {
        "output_bytes": len(rendered.encode("utf-8")),
        **delta,
    }


async def _benchmark(samples: int, warmups: int) -> dict[str, object]:
    from agent_eyes.server import TOOLS

    known_latencies: list[float] = []
    discovery_latencies: list[float] = []
    known_outputs: list[int] = []
    discovery_outputs: list[int] = []
    observe_outputs: list[int] = []
    known_evidence: dict[str, set[int]] = {}
    discovery_evidence: dict[str, set[int]] = {}
    observe_evidence: dict[str, set[int]] = {}
    reveal_evidence: dict[str, set[int]] = {}

    async with TransactionFixture() as fixture:
        for index in range(samples + warmups):
            known = await _known_scenario(fixture)
            discovery = await _discovery_scenario(fixture)
            reveal = await _reveal_scenario(fixture)
            observed = await _observe_budget_scenario(fixture)
            if index < warmups:
                continue
            known_latencies.append(float(known.pop("elapsed_ms")))
            discovery_latencies.append(float(discovery.pop("elapsed_ms")))
            known_outputs.append(int(known.pop("output_bytes")))
            discovery_outputs.append(int(discovery.pop("output_bytes")))
            observe_outputs.append(int(discovery.pop("observe_output_bytes")))
            discovery_outputs.append(int(reveal.pop("output_bytes")))
            observe_outputs.append(int(observed.pop("output_bytes")))
            _record(known_evidence, known)
            _record(discovery_evidence, discovery)
            _record(reveal_evidence, reveal)
            _record(observe_evidence, observed)

        retained_resources = fixture.resources()

    catalog_bytes = len(
        json.dumps(
            [tool.model_dump(mode="json", exclude_none=True) for tool in TOOLS],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    evidence = {
        "known": {key: sorted(values) for key, values in known_evidence.items()},
        "discovery": {
            key: sorted(values) for key, values in discovery_evidence.items()
        },
        "observe": {key: sorted(values) for key, values in observe_evidence.items()},
        "reveal": {key: sorted(values) for key, values in reveal_evidence.items()},
    }
    execute_max = max(known_outputs + discovery_outputs)
    observe_max = max(observe_outputs)
    deterministic_passed = bool(
        evidence["known"]["mcp_calls"] == [1]
        and evidence["discovery"]["mcp_calls"] == [2]
        and evidence["reveal"]["mcp_calls"] == [1]
        and max(evidence["known"]["full_scans_before_first_mutation"]) <= 1
        and max(evidence["discovery"]["full_scans_before_first_mutation"]) <= 1
        and max(evidence["known"]["max_activations_per_call"]) <= 1
        and max(evidence["discovery"]["max_activations_per_call"]) <= 1
        and max(evidence["reveal"]["full_scans_before_first_mutation"]) <= 1
        and max(evidence["reveal"]["max_activations_per_call"]) <= 1
        and evidence["known"]["external_writes"] == [1]
        and evidence["discovery"]["external_writes"] == [1]
        and evidence["known"]["shadow_probes"] == [0]
        and evidence["discovery"]["shadow_probes"] == [0]
        and evidence["reveal"]["external_writes"] == [0]
        and evidence["reveal"]["shadow_probes"] == [0]
        and evidence["known"]["event_registrations"]
        == evidence["known"]["event_closes"]
        and evidence["discovery"]["event_registrations"]
        == evidence["discovery"]["event_closes"]
        and execute_max <= EXECUTE_OUTPUT_GATE_BYTES
        and observe_max <= OBSERVE_OUTPUT_GATE_BYTES
        and catalog_bytes <= CATALOG_GATE_BYTES
    )
    return {
        "schema_version": 2,
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
            "fixture": "deterministic Bitbucket-like foreground transaction; no network or rendering latency",
            "fixed_orchestration_sleeps": 0,
        },
        "reference_latency": {
            "known": _summary(known_latencies),
            "discovery": _summary(discovery_latencies),
            "targets_ms": {
                "median": REFERENCE_MEDIAN_TARGET_MS,
                "p95": REFERENCE_P95_TARGET_MS,
            },
            "release_gating": False,
        },
        "output": {
            "execute_max_bytes": execute_max,
            "observe_target_max_bytes": observe_max,
            "catalog_bytes": catalog_bytes,
        },
        "evidence": evidence,
        "retained_resources_before_fixture_close": retained_resources,
        "gates": {
            "execute_bytes": EXECUTE_OUTPUT_GATE_BYTES,
            "observe_target_bytes": OBSERVE_OUTPUT_GATE_BYTES,
            "catalog_bytes": CATALOG_GATE_BYTES,
            "passed": deterministic_passed,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=_positive_integer, default=30)
    parser.add_argument("--warmups", type=_non_negative_integer, default=3)
    arguments = parser.parse_args()
    result = asyncio.run(_benchmark(arguments.samples, arguments.warmups))
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["gates"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
