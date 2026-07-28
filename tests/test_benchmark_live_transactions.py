from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from benchmarks import benchmark_live_transactions


_ORIGINAL_ID = "native:42:w0:t0:r1111111111111111"
_TARGET_ID = "native:42:w0:t1:r2222222222222222"


def _result(text: str):
    return SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text=text)],
    )


def _inventory(target_id: str, browser: str, title: str) -> str:
    return (
        "Scanned 2 open browser targets across 1 browser processes using foreground accessibility.\n"
        f"[{target_id}] {browser} pid=42 tab=0 (selected,frontmost) — {title}"
    )


def _install_fake_session(monkeypatch, *, restore_exact: bool):
    calls: list[tuple[str, dict[str, object]]] = []
    inventory_calls = 0

    class Transport:
        async def __aenter__(self):
            return "read", "write"

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, read_stream, write_stream):
            assert (read_stream, write_stream) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(version="0.10.0"))

        async def call_tool(self, name, arguments):
            nonlocal inventory_calls
            calls.append((name, arguments))
            if name == "list_tabs":
                inventory_calls += 1
                if inventory_calls == 2:
                    return _result(
                        _inventory(_TARGET_ID, "Mozilla Firefox", "Reference fixture")
                    )
                return _result(_inventory(_ORIGINAL_ID, "Safari", "Original"))
            if name == "execute":
                return _result(
                    json.dumps(
                        {
                            "status": "succeeded",
                            "target_id": arguments["target"]["target_id"],
                        }
                    )
                )
            chosen = arguments.get("target_id", _TARGET_ID)
            if chosen == _ORIGINAL_ID and not restore_exact:
                chosen = _TARGET_ID
            return _result(
                json.dumps(
                    {
                        "status": "ok",
                        "target": {"id": chosen},
                        "scan": {"inventory_cache": "hit"},
                    }
                )
            )

    clock = {"now": 0.0}

    def perf_counter():
        clock["now"] += 0.001
        return clock["now"]

    monkeypatch.setattr(
        benchmark_live_transactions,
        "stdio_client",
        lambda *_args, **_kwargs: Transport(),
    )
    monkeypatch.setattr(benchmark_live_transactions, "ClientSession", Session)
    monkeypatch.setattr(
        benchmark_live_transactions.time,
        "perf_counter",
        perf_counter,
    )
    return calls


def _benchmark(*, browser_version: str = ""):
    return asyncio.run(
        benchmark_live_transactions._benchmark(
            executable=Path(benchmark_live_transactions.__file__),
            query="confidential query redacted by result",
            samples=3,
            warmups=1,
            deadline_ms=3_000,
            browser_version=browser_version,
        )
    )


def test_inventory_parser_returns_count_and_selected_native_target() -> None:
    text = _inventory(_TARGET_ID, "Mozilla Firefox", "Agent Eyes")

    assert benchmark_live_transactions._inventory_evidence(text) == (2, _TARGET_ID)
    assert benchmark_live_transactions._browser_name(text, _TARGET_ID) == (
        "Mozilla Firefox"
    )


def test_optional_browser_version_accepts_the_cli_default() -> None:
    assert benchmark_live_transactions._browser_version("") == ""


def test_live_mcp_benchmark_separates_reference_latency_from_deterministic_gates(
    monkeypatch,
) -> None:
    calls = _install_fake_session(monkeypatch, restore_exact=True)

    result = _benchmark(browser_version="128.0")

    assert result["environment"]["agent_eyes"] == "0.10.0"
    assert result["environment"]["browser"] == {
        "name": "Mozilla Firefox",
        "version": "128.0",
    }
    assert result["protocol"]["query_redacted"] is True
    assert result["protocol"]["shadow_mode_requested"] is False
    assert result["protocol"]["latency_gate_enforced"] is False
    assert result["query_resolution_ms"] == 1.0
    assert result["exact_latency"] == {
        "median_ms": 1.0,
        "p95_ms": 1.0,
        "max_ms": 1.0,
    }
    assert result["cache_statuses"] == {"hit": 4}
    assert result["counts"] == {
        "inventory_calls": 3,
        "query_resolution_calls": 1,
        "warmup_exact_calls": 1,
        "measured_exact_calls": 3,
        "exact_execute_calls": 0,
        "exact_observe_calls": 4,
        "focus_restore_calls": 1,
        "shadow_mode_requests": 0,
    }
    assert result["output"]["max_observe_target_bytes"] < 4 * 1024
    assert result["output"]["max_exact_call_bytes"] < 4 * 1024
    assert result["output"]["exact_call_limit_bytes"] == 4 * 1024
    assert result["original_target_restored"] is True
    assert result["targets_preserved"] is True
    assert result["gates"] == {
        "deterministic_passed": True,
        "reference_latency_passed": True,
        "reference_latency_enforced": False,
        "passed": True,
    }
    assert [name for name, _arguments in calls].count("observe_target") == 6
    assert [name for name, _arguments in calls].count("list_tabs") == 3
    assert "confidential query" not in json.dumps(result)


def test_live_reference_can_measure_a_read_only_execute_expect_journey(
    monkeypatch,
) -> None:
    calls = _install_fake_session(monkeypatch, restore_exact=True)

    result = asyncio.run(
        benchmark_live_transactions._benchmark(
            executable=Path(benchmark_live_transactions.__file__),
            query="redacted",
            samples=3,
            warmups=1,
            deadline_ms=3_000,
            selector_role="heading",
            selector_name="Reference fixture",
        )
    )

    assert result["protocol"]["exact_call"] == "execute_expect"
    assert result["counts"]["exact_execute_calls"] == 4
    assert result["counts"]["exact_observe_calls"] == 0
    assert result["output"]["exact_call_limit_bytes"] == 2 * 1024
    assert result["provider_phases"]["exact_call"]["median_ms"] == 1.0
    assert [name for name, _arguments in calls].count("execute") == 4


def test_focus_restore_requires_the_returned_exact_original_identifier(
    monkeypatch,
) -> None:
    _install_fake_session(monkeypatch, restore_exact=False)

    result = _benchmark()

    assert result["original_target_restored"] is False
    assert result["gates"]["deterministic_passed"] is False
    assert result["gates"]["passed"] is False


def test_reference_latency_is_enforced_only_when_explicitly_requested(
    monkeypatch,
) -> None:
    _install_fake_session(monkeypatch, restore_exact=True)
    monkeypatch.setattr(
        benchmark_live_transactions,
        "REFERENCE_MEDIAN_GATE_MS",
        0.5,
    )

    reported = _benchmark()
    enforced = asyncio.run(
        benchmark_live_transactions._benchmark(
            executable=Path(benchmark_live_transactions.__file__),
            query="redacted",
            samples=3,
            warmups=1,
            deadline_ms=3_000,
            enforce_reference_latency=True,
        )
    )

    assert reported["gates"]["reference_latency_passed"] is False
    assert reported["gates"]["passed"] is True
    assert enforced["protocol"]["latency_gate_enforced"] is True
    assert enforced["gates"]["passed"] is False
