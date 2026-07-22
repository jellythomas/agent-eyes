from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

from mcp.types import CallToolResult

from agent_eyes.setup.readiness import probe_readiness


class AvailableNative:
    def is_available(self) -> bool:
        return True

    def check_permissions(self) -> tuple[bool, str]:
        return True, "granted"


class AvailableInput:
    def is_available(self) -> bool:
        return True


def _ready_report():
    return probe_readiness(
        native_provider=AvailableNative(),
        input_provider=AvailableInput(),
        persistent_executable=sys.executable,
    )


def _blocked_report():
    return probe_readiness(
        native_provider=None,
        input_provider=AvailableInput(),
        persistent_executable=sys.executable,
    )


def test_server_import_does_not_construct_or_probe_runtime_providers():
    program = r'''
import json
import sys
from agent_eyes.setup import readiness

calls = []

def record(name):
    def unexpected(*args, **kwargs):
        calls.append(name)
        raise AssertionError(f"{name} ran during import")
    return unexpected

readiness.load_native_provider = record("native")
readiness.load_input_provider = record("input")
readiness.probe_readiness = record("probe")

import agent_eyes.server as server

print(json.dumps({
    "calls": calls,
    "native_adapter": server.native_adapter is None,
    "input_backend": server._input_backend is None,
    "readiness": server._runtime_readiness is None,
    "native_events_imported": "agent_eyes.native_events" in sys.modules,
    "browser_inventory_imported": "agent_eyes.browser_inventory" in sys.modules,
    "application_services_imported": "ApplicationServices" in sys.modules,
    "quartz_imported": "Quartz" in sys.modules,
}))
'''

    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "calls": [],
        "native_adapter": True,
        "input_backend": True,
        "readiness": True,
        "native_events_imported": False,
        "browser_inventory_imported": False,
        "application_services_imported": False,
        "quartz_imported": False,
    }


def test_tools_list_is_static_and_does_not_await_readiness(monkeypatch):
    from agent_eyes import server

    async def unexpected_readiness(*, refresh: bool = False):
        raise AssertionError(f"tools/list awaited readiness (refresh={refresh})")

    monkeypatch.setattr(server, "_ensure_runtime_readiness", unexpected_readiness)

    tools = asyncio.run(server.list_tools())

    assert tools is server.TOOLS
    assert {tool.name for tool in tools} >= {"status", "list_apps", "tree", "list_tabs"}


def test_first_capability_action_awaits_readiness_before_dispatch(monkeypatch):
    from agent_eyes import server

    order: list[str] = []

    async def ensure_readiness(*, refresh: bool = False):
        assert refresh is False
        order.append("readiness")
        return _ready_report()

    async def dispatch(name: str, arguments: dict):
        order.append("dispatch")
        return f"{name}:{arguments['pid']}"

    monkeypatch.setattr(server, "_ensure_runtime_readiness", ensure_readiness)
    monkeypatch.setattr(server, "_dispatch", dispatch)

    result = asyncio.run(server.call_tool("tree", {"pid": 42}))

    assert order == ["readiness", "dispatch"]
    assert result[0].text == "tree:42"


def test_capability_action_fails_closed_before_dispatch_when_runtime_is_blocked(monkeypatch):
    from agent_eyes import server

    dispatched = False

    async def blocked_readiness(*, refresh: bool = False):
        assert refresh is False
        return _blocked_report()

    async def unexpected_dispatch(name: str, arguments: dict):
        nonlocal dispatched
        dispatched = True
        return "unsafe"

    monkeypatch.setattr(server, "_ensure_runtime_readiness", blocked_readiness)
    monkeypatch.setattr(server, "_dispatch", unexpected_dispatch)

    result = asyncio.run(server.call_tool("tree", {"pid": 42}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "setup_required" in result.content[0].text
    assert dispatched is False


def test_concurrent_first_actions_construct_and_probe_providers_once(monkeypatch):
    from agent_eyes import server

    counts = {"native": 0, "input": 0, "native_probe": 0, "input_probe": 0}
    operations = {"native": [], "input": []}
    native = AvailableNative()
    input_backend = AvailableInput()
    report = _ready_report()

    class OffLoopWorker:
        def __init__(self, lane: str) -> None:
            self._lane = lane

        async def run(self, function, *, budget, operation):
            assert budget.remaining() > 0
            operations[self._lane].append(operation)
            return await asyncio.to_thread(function)

    def load_native():
        counts["native"] += 1
        return native

    def load_input():
        counts["input"] += 1
        return input_backend

    def native_probe(native_provider):
        counts["native_probe"] += 1
        assert native_provider is native
        return report.capability("native_access")

    def input_probe(provider):
        counts["input_probe"] += 1
        assert provider is input_backend
        return report.capability("input")

    monkeypatch.setattr(server, "native_adapter", None)
    monkeypatch.setattr(server, "_input_backend", None)
    monkeypatch.setattr(server, "_runtime_readiness", None)
    monkeypatch.setattr(server, "_get_native_adapter", load_native)
    monkeypatch.setattr(server, "load_input_provider", load_input)
    monkeypatch.setattr(server, "probe_native_capability", native_probe)
    monkeypatch.setattr(server, "probe_input_capability", input_probe)
    monkeypatch.setattr(server, "native_worker", OffLoopWorker("native"))
    monkeypatch.setattr(server, "input_worker", OffLoopWorker("input"))

    async def run():
        return await asyncio.gather(
            *(server._ensure_runtime_readiness() for _ in range(32))
        )

    reports = asyncio.run(run())

    assert all(item is reports[0] for item in reports)
    assert reports[0].core_ready is True
    assert counts == {"native": 1, "input": 1, "native_probe": 1, "input_probe": 1}
    assert operations == {
        "native": ["native provider construction", "native readiness probe"],
        "input": ["input provider construction", "input readiness probe"],
    }


def test_each_readiness_probe_runs_on_its_provider_owner_lane(monkeypatch):
    from agent_eyes import server

    class ThreadAffineNative(AvailableNative):
        def __init__(self) -> None:
            self.owner = threading.get_ident()

        def is_available(self) -> bool:
            assert threading.get_ident() == self.owner
            return True

        def check_permissions(self) -> tuple[bool, str]:
            assert threading.get_ident() == self.owner
            return True, "granted"

    class ThreadAffineInput(AvailableInput):
        def __init__(self) -> None:
            self.owner = threading.get_ident()

        def is_available(self) -> bool:
            assert threading.get_ident() == self.owner
            return True

    class ThreadLaneWorker:
        def __init__(self, name: str) -> None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)

        async def run(self, function, *, budget, operation):
            assert budget.remaining() > 0
            return await asyncio.get_running_loop().run_in_executor(
                self.executor,
                function,
            )

    native_lane = ThreadLaneWorker("native-readiness-test")
    input_lane = ThreadLaneWorker("input-readiness-test")
    monkeypatch.setattr(server, "native_adapter", None)
    monkeypatch.setattr(server, "_input_backend", None)
    monkeypatch.setattr(server, "_runtime_readiness", None)
    monkeypatch.setattr(server, "_get_native_adapter", ThreadAffineNative)
    monkeypatch.setattr(server, "load_input_provider", ThreadAffineInput)
    monkeypatch.setattr(server, "native_worker", native_lane)
    monkeypatch.setattr(server, "input_worker", input_lane)

    try:
        report = asyncio.run(server._ensure_runtime_readiness())
    finally:
        native_lane.executor.shutdown(wait=True)
        input_lane.executor.shutdown(wait=True)

    assert report.core_ready is True


def test_slow_first_provider_load_does_not_block_event_loop(monkeypatch):
    from agent_eyes import server

    started = threading.Event()
    release = threading.Event()

    class OffLoopWorker:
        async def run(self, function, *, budget, operation):
            return await asyncio.to_thread(function)

    def load_native():
        started.set()
        assert release.wait(timeout=1)
        return AvailableNative()

    monkeypatch.setattr(server, "native_adapter", None)
    monkeypatch.setattr(server, "_input_backend", None)
    monkeypatch.setattr(server, "_runtime_readiness", None)
    monkeypatch.setattr(server, "_get_native_adapter", load_native)
    monkeypatch.setattr(server, "load_input_provider", AvailableInput)
    monkeypatch.setattr(server, "native_worker", OffLoopWorker())
    monkeypatch.setattr(server, "input_worker", OffLoopWorker())

    async def run():
        initialization = asyncio.create_task(server._ensure_runtime_readiness())
        while not started.is_set():
            await asyncio.sleep(0)

        heartbeat_completed = False

        async def heartbeat():
            nonlocal heartbeat_completed
            await asyncio.sleep(0)
            heartbeat_completed = True

        await asyncio.wait_for(heartbeat(), timeout=0.1)
        assert heartbeat_completed is True
        release.set()
        return await asyncio.wait_for(initialization, timeout=1)

    report = asyncio.run(run())

    assert report.core_ready is True


def test_runtime_shutdown_closes_coordinator_browser_and_all_provider_lanes(monkeypatch):
    from agent_eyes import server

    coordinator = MagicMock(close=AsyncMock())
    pool = MagicMock(disconnect=AsyncMock())
    workers = [MagicMock(aclose=AsyncMock()) for _ in range(4)]
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(server, "native_worker", workers[0])
    monkeypatch.setattr(server, "input_worker", workers[1])
    monkeypatch.setattr(server, "apple_worker", workers[2])
    monkeypatch.setattr(server, "system_worker", workers[3])

    asyncio.run(server._shutdown_runtime())

    coordinator.close.assert_awaited_once_with()
    pool.disconnect.assert_awaited_once_with()
    for worker in workers:
        worker.aclose.assert_awaited_once_with()
