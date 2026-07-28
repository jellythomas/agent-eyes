from __future__ import annotations

import asyncio

from benchmarks import stress_transactions


def test_cancellation_at_every_named_transaction_boundary_is_safe() -> None:
    evidence = asyncio.run(stress_transactions._cancellation_boundary_evidence())

    assert evidence == {
        "target_resolution": {
            "terminal": "cancelled",
            "dispatches": 0,
            "refreshes": 0,
            "completed_steps": None,
            "failed_step": None,
            "retry_safe": None,
        },
        "initial_observation": {
            "terminal": "cancelled",
            "dispatches": 0,
            "refreshes": 0,
            "completed_steps": None,
            "failed_step": None,
            "retry_safe": None,
        },
        "capability_preflight": {
            "terminal": "cancelled",
            "dispatches": 0,
            "refreshes": 0,
            "completed_steps": None,
            "failed_step": None,
            "retry_safe": None,
        },
        "focus_preflight": {
            "terminal": "cancelled",
            "dispatches": 0,
            "refreshes": 0,
            "completed_steps": None,
            "failed_step": None,
            "retry_safe": None,
        },
        "provider_dispatch": {
            "terminal": "outcome_unknown",
            "dispatches": 1,
            "refreshes": 0,
            "completed_steps": 1,
            "failed_step": 2,
            "retry_safe": False,
        },
        "scoped_refresh": {
            "terminal": "outcome_unknown",
            "dispatches": 1,
            "refreshes": 1,
            "completed_steps": 1,
            "failed_step": 2,
            "retry_safe": False,
        },
        "final_expectation": {
            "terminal": "outcome_unknown",
            "dispatches": 1,
            "refreshes": 1,
            "completed_steps": 2,
            "failed_step": None,
            "retry_safe": False,
        },
    }


def test_32_queued_foreground_transactions_never_overlap_or_dispatch_cancelled() -> (
    None
):
    evidence = asyncio.run(
        stress_transactions._foreground_queue_evidence(
            concurrency=32,
            cancellation_count=16,
        )
    )

    assert evidence["entered_queue"] == 32
    assert evidence["completed"] == 16
    assert evidence["max_active_operations"] == 1
    assert evidence["dispatches"] == 16
    assert evidence["duplicate_dispatches"] == 0
    assert evidence["cancelled_dispatches"] == 0
    assert evidence["queue_locked_after"] is False


def test_ten_thousand_cycles_retain_no_resources_and_meet_rss_gate() -> None:
    async def run():
        resources = await stress_transactions._resource_retention_evidence(10_000)
        rss = stress_transactions._snapshot_rss_cycles(10_000)
        return resources, rss

    resources, rss = asyncio.run(run())

    assert resources == {
        "cycles": 10_000,
        "released_references": 10_000,
        "live_provider_references": 0,
        "opened_subscriptions": 10_000,
        "closed_subscriptions": 10_000,
        "live_subscriptions": 0,
        "threaded_subscription_cycles": 32,
        "live_threaded_subscriptions": 0,
        "native_event_threads": 0,
        "workers_clean_before_close": True,
        "live_workers_after_close": 0,
        "snapshots": 0,
        "resolver_cache_entries": 0,
        "resolver_lease_entries": 0,
        "resolver_flights": 0,
        "coordinator_flights": 0,
        "coordinator_shadow_locks": 0,
        "coordinator_poison": 0,
        "foreground_locked": False,
        "inventory_calls": 10_000,
    }
    assert rss["cycles"] == 10_000
    assert rss["released_records"] == 10_000
    assert rss["leaked_snapshots"] == 0
    assert rss["maximum_live_snapshots"] == 1
    assert rss["threshold_bytes"] == max(
        10 * 1024 * 1024,
        (rss["baseline_rss_bytes"] + 19) // 20,
    )
    assert rss["growth_bytes"] <= rss["threshold_bytes"]
