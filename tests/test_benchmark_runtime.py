from pathlib import Path

from benchmarks import benchmark_runtime


def test_runtime_benchmark_has_no_fixed_orchestration_sleep_calls():
    assert benchmark_runtime._fixed_orchestration_sleep_calls() == []


def test_sleep_scanner_detects_module_and_direct_import_aliases(tmp_path: Path):
    source = tmp_path / "orchestration.py"
    source.write_text(
        "import asyncio as aio\n"
        "from time import sleep as pause\n"
        "\n"
        "async def run():\n"
        "    await aio.sleep(1)\n"
        "    pause(1)\n",
        encoding="utf-8",
    )

    assert benchmark_runtime._sleep_calls_in((source,)) == [
        f"{source}:5",
        f"{source}:6",
    ]


def test_static_runtime_gates_cover_only_deterministic_context_and_sleep_counts():
    gates = benchmark_runtime._deterministic_static_gates()

    assert gates["fixed_orchestration_sleep_calls"] == 0
    assert gates["tools_list_compact_json_bytes"] <= 16 * 1024
    assert gates["tool_catalog_limit_bytes"] == 16 * 1024
    assert gates["passed"] is True
    assert not any("latency" in key for key in gates)


def test_static_runtime_gate_fails_on_a_fixed_orchestration_sleep(monkeypatch):
    monkeypatch.setattr(
        benchmark_runtime,
        "_fixed_orchestration_sleep_calls",
        lambda: ["server.py:1"],
    )

    gates = benchmark_runtime._deterministic_static_gates()

    assert gates["fixed_orchestration_sleep_calls"] == 1
    assert gates["passed"] is False
