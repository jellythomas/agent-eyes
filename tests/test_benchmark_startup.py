from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

from benchmarks import benchmark_startup


def test_cold_import_uses_the_fresh_interpreter_reported_duration(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="123.5\n")

    monkeypatch.setattr(benchmark_startup.subprocess, "run", run)

    assert benchmark_startup._cold_import_sample() == 123.5
    assert observed["command"][:2] == [benchmark_startup.sys.executable, "-c"]
    assert "time.perf_counter" in observed["command"][2]
    assert observed["kwargs"]["text"] is True


def test_mcp_latency_stops_at_tools_list_response_before_transport_cleanup(
    monkeypatch,
):
    clock = {"now": 0.0}

    class Transport:
        async def __aenter__(self):
            return "read", "write"

        async def __aexit__(self, *_args):
            clock["now"] = 20.0

    class Session:
        def __init__(self, read_stream, write_stream):
            assert (read_stream, write_stream) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            clock["now"] = 10.0

        async def initialize(self):
            clock["now"] = 0.3

        async def list_tools(self):
            clock["now"] = 0.4
            return SimpleNamespace(tools=[object(), object()])

    monkeypatch.setattr(
        benchmark_startup,
        "stdio_client",
        lambda *_args, **_kwargs: Transport(),
    )
    monkeypatch.setattr(benchmark_startup, "ClientSession", Session)
    monkeypatch.setattr(benchmark_startup.time, "perf_counter", lambda: clock["now"])

    elapsed, tool_count = asyncio.run(benchmark_startup._mcp_tools_list_sample(None))

    assert elapsed == 400.0
    assert tool_count == 2
    assert clock["now"] == 20.0
