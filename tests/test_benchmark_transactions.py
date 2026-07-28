from __future__ import annotations

import asyncio

from agent_eyes.transaction_contract import MAX_TRANSACTION_STEPS
from benchmarks import benchmark_transactions
from benchmarks.transaction_fixture import post_arguments, reveal_arguments


def test_shared_scenarios_preserve_the_eight_step_public_contract() -> None:
    assert len(reveal_arguments()["steps"]) <= MAX_TRANSACTION_STEPS
    assert len(post_arguments()["steps"]) <= MAX_TRANSACTION_STEPS


def test_deterministic_transaction_counts_and_output_gates() -> None:
    result = asyncio.run(benchmark_transactions._benchmark(samples=8, warmups=2))

    assert result["gates"]["passed"] is True
    assert result["output"]["execute_max_bytes"] <= 2 * 1024
    assert result["output"]["observe_target_max_bytes"] <= 4 * 1024
    assert result["output"]["catalog_bytes"] <= 16 * 1024

    known = result["evidence"]["known"]
    discovery = result["evidence"]["discovery"]
    reveal = result["evidence"]["reveal"]
    assert known["mcp_calls"] == [1]
    assert discovery["mcp_calls"] == [2]
    assert reveal["mcp_calls"] == [1]
    assert known["external_writes"] == [1]
    assert discovery["external_writes"] == [1]
    assert known["shadow_probes"] == [0]
    assert discovery["shadow_probes"] == [0]
    assert reveal["external_writes"] == [0]
    assert reveal["shadow_probes"] == [0]
    assert max(known["full_scans_before_first_mutation"]) <= 1
    assert max(discovery["full_scans_before_first_mutation"]) <= 1
    assert max(known["max_activations_per_call"]) <= 1
    assert max(discovery["max_activations_per_call"]) <= 1
    assert max(reveal["full_scans_before_first_mutation"]) <= 1
    assert max(reveal["max_activations_per_call"]) <= 1
    assert known["event_registrations"] == known["event_closes"]
    assert discovery["event_registrations"] == discovery["event_closes"]


def test_reference_latency_is_recorded_but_not_a_deterministic_ci_gate() -> None:
    result = asyncio.run(benchmark_transactions._benchmark(samples=3, warmups=1))

    assert result["reference_latency"]["release_gating"] is False
    assert result["reference_latency"]["targets_ms"] == {
        "median": 1_000.0,
        "p95": 3_000.0,
    }
    assert result["reference_latency"]["known"]["median_ms"] >= 0
    assert result["reference_latency"]["discovery"]["p95_ms"] >= 0
