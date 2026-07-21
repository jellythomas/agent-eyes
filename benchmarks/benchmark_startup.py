"""Repeatable cold-start and MCP tools/list benchmarks for Agent Eyes."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[p95_index], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _cold_import_sample() -> float:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import time; "
                "started = time.perf_counter(); "
                "import agent_eyes.server; "
                "print((time.perf_counter() - started) * 1000)"
            ),
        ],
        check=True,
        cwd=_PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    return float(completed.stdout.strip())


async def _mcp_tools_list_sample(errlog: TextIO) -> tuple[float, int]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_eyes.server"],
        cwd=_PROJECT_ROOT,
    )
    started = time.perf_counter()
    async with stdio_client(parameters, errlog=errlog) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            elapsed = (time.perf_counter() - started) * 1000
    return elapsed, len(result.tools)


async def _benchmark(samples: int, warmups: int) -> dict[str, object]:
    import_latencies = [
        _cold_import_sample()
        for _ in range(samples + warmups)
    ][warmups:]

    mcp_latencies: list[float] = []
    tool_counts: set[int] = set()
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        for index in range(samples + warmups):
            elapsed, tool_count = await _mcp_tools_list_sample(errlog)
            if index >= warmups:
                mcp_latencies.append(elapsed)
                tool_counts.add(tool_count)

    return {
        "schema_version": 2,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "protocol": {
            "warmups": warmups,
            "samples": samples,
            "percentile": "sorted[ceil(0.95*n)-1]",
            "server_import_clock": "inside fresh interpreter; process lifecycle excluded",
            "mcp_clock": "process launch through tools/list response; cleanup excluded",
        },
        "latency": {
            "server_import": _summary(import_latencies),
            "mcp_initialize_and_tools_list": _summary(mcp_latencies),
        },
        "tool_counts": sorted(tool_counts),
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=_positive_integer, default=30)
    parser.add_argument("--warmups", type=_non_negative_integer, default=3)
    arguments = parser.parse_args()
    result = asyncio.run(_benchmark(arguments.samples, arguments.warmups))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
