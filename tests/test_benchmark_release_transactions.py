from __future__ import annotations

import asyncio

from benchmarks import benchmark_release_transactions


def test_32_way_public_execute_queue_serializes_and_cancels_before_dispatch() -> None:
    evidence = asyncio.run(
        benchmark_release_transactions._foreground_queue_evidence(
            concurrency=32,
            cancellation_count=16,
        )
    )

    assert evidence == {
        "concurrency": 32,
        "cancelled": 16,
        "completed": 16,
        "entered_queue": 32,
        "started_operations": 16,
        "completed_operations": 16,
        "max_active_operations": 1,
        "tree_reads": 16,
        "dispatches": 16,
        "duplicate_dispatches": 0,
        "cancelled_dispatches": 0,
        "queue_locked_after": False,
    }


def test_deep_snapshot_retention_has_an_exact_bounded_memory_proxy() -> None:
    evidence = benchmark_release_transactions._snapshot_retention_evidence(
        cycles=64,
        tree_depth=384,
        capacity=4,
        payload_bytes=512,
    )

    assert evidence == {
        "cycles": 64,
        "tree_depth": 384,
        "capacity": 4,
        "payload_bytes": 512,
        "max_live_snapshots": 4,
        "live_payloads_before_close": 1_536,
        "live_payloads_after_close": 0,
        "retained_payload_bytes_proxy": 786_432,
        "proxy_gate_bytes": 786_432,
        "released_records": 24_576,
        "leaked_snapshots": 0,
    }


def test_public_execute_browser_fixture_inventories_and_activates_exact_target() -> (
    None
):
    evidence = asyncio.run(benchmark_release_transactions._browser_target_evidence())

    assert evidence["inventory_calls"] == 1
    assert evidence["inventory_targets"] == 3
    assert evidence["activation_calls"] == 1
    assert evidence["activated_target_id"] == evidence["expected_target_id"]
    assert evidence["activated_browser"] == "Firefox"
    assert evidence["scoped_reads"] == 1
    assert evidence["full_reads"] == 0
    assert evidence["completed_steps"] == 1
    assert evidence["status"] == "succeeded"
    assert evidence["implicit_shadow_probes"] == 0


def test_combined_release_benchmark_passes_every_evidence_gate() -> None:
    result = asyncio.run(benchmark_release_transactions._benchmark())

    assert result["schema_version"] == 1
    assert result["protocol"] == {
        "fixture": "deterministic public execute and in-process retention evidence",
        "network_calls": 0,
        "live_ui_calls": 0,
        "fixed_sleeps": 0,
    }
    assert result["gates"] == {"passed": True}
