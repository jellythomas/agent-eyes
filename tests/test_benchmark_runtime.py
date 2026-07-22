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
