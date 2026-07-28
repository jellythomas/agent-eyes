from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.adapters.base import UIElement
from agent_eyes.observations import ElementRecord
from agent_eyes.operation import OperationError, OperationErrorCode, OperationMode


class _InlineWorker:
    async def run(self, function, *, budget, operation, state=None):
        assert budget.remaining() > 0
        assert operation
        return function()


class _RunningApp:
    def __init__(self, *, activate: bool, terminate: bool) -> None:
        self.activateWithOptions_ = MagicMock(return_value=activate)
        self.terminate = MagicMock(return_value=terminate)

    def localizedName(self) -> str:
        return "Demo"

    def bundleIdentifier(self) -> str:
        return "com.example.demo"

    def processIdentifier(self) -> int:
        return 42


@pytest.mark.parametrize("action", ["focus", "quit"])
def test_app_mutation_reports_native_rejection(monkeypatch, action):
    from agent_eyes import server

    running = _RunningApp(activate=False, terminate=False)
    workspace = SimpleNamespace(runningApplications=lambda: [running])
    appkit = ModuleType("AppKit")
    appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: workspace)
    appkit.NSWorkspaceOpenConfiguration = object
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(server.sys, "platform", "darwin")

    result = server._handle_app({"action": action, "name": "Demo"})

    assert result == f"ERROR: App 'Demo' rejected the {action} request."


def test_successful_app_action_invalidates_native_observations(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:42",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=1, value="old")],
    )
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "system_worker", _InlineWorker())
    monkeypatch.setattr(server, "_handle_app", lambda _args: "Focused 'Demo' (PID 42).")

    result = asyncio.run(server._handle_app_async({"action": "focus", "name": "Demo"}))

    assert result == "Focused 'Demo' (PID 42)."
    with pytest.raises(OperationError) as exc_info:
        coordinator.observations.resolve(snapshot.token, 1)
    assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT


class _FakeAX:
    def __init__(self, error: int) -> None:
        self._error = error

    def AXUIElementCreateApplication(self, _pid: int):
        return "app"

    def AXUIElementPerformAction(self, _element, _action: str) -> int:
        return self._error

    def AXUIElementSetAttributeValue(self, _element, _attribute: str, _value) -> int:
        return self._error


def _install_window_shims(monkeypatch, server, *, error: int):
    application_services = ModuleType("ApplicationServices")
    application_services.AXValueCreate = lambda kind, value: (kind, value)
    application_services.kAXValueCGPointType = "point"
    application_services.kAXValueCGSizeType = "size"
    quartz = ModuleType("Quartz")
    quartz.CGPoint = lambda x, y: (x, y)
    quartz.CGSize = lambda width, height: (width, height)
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    adapter = SimpleNamespace(_ax=_FakeAX(error))
    adapter._read_attr = lambda _element, attribute: (
        "close-button" if attribute == "AXCloseButton" else "window"
    )
    adapter.is_element_valid = lambda _element: True
    adapter.focus_window = lambda _element: error == 0
    adapter.is_window_focused = lambda _element: error == 0
    coordinator = AutomationCoordinator()
    window_element = UIElement(
        id=91,
        role="window",
        name="Exact window",
        platform_ref="window",
        pid=42,
    )
    target = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:42",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=91, value=window_element)],
    )

    async def action_until(_pid, action, condition, **_kwargs):
        return SimpleNamespace(
            action_result=action(),
            condition_met=condition(),
        )

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "_input_backend", None)
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "run_native_action_until", action_until)
    return coordinator, target.token, window_element.id


@pytest.mark.parametrize(
    "args",
    [
        {"action": "focus", "pid": 42},
        {"action": "minimize", "pid": 42},
        {"action": "close", "pid": 42},
        {"action": "move", "pid": 42, "x": 10, "y": 20},
        {"action": "resize", "pid": 42, "width": 800, "height": 600},
    ],
)
def test_window_mutations_report_ax_error_codes(monkeypatch, args):
    from agent_eyes import server

    _coordinator, token, window_id = _install_window_shims(
        monkeypatch,
        server,
        error=-25205,
    )
    args = {**args, "snapshot": token, "id": window_id}

    result = asyncio.run(server._handle_window(args))

    assert result.startswith("ERROR:")
    assert "rejected" in result or "verify exact window focus" in result


def test_successful_window_mutation_invalidates_exact_pid_snapshot(monkeypatch):
    from agent_eyes import server

    coordinator, token, window_id = _install_window_shims(
        monkeypatch,
        server,
        error=0,
    )
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:42",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=1, value="old")],
    )
    result = asyncio.run(
        server._handle_window(
            {
                "action": "minimize",
                "pid": 42,
                "snapshot": token,
                "id": window_id,
            }
        )
    )

    assert result == "Minimized window [91] for PID 42."
    with pytest.raises(OperationError) as exc_info:
        coordinator.observations.resolve(snapshot.token, 1)
    assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT


def test_window_mutation_rejects_pid_only_before_native_lookup(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(server._handle_window({"action": "close", "pid": 42}))

    assert "snapshot and id" in result
    adapter.is_element_valid.assert_not_called()


def test_window_list_returns_exact_snapshot_and_id_targets(monkeypatch):
    from agent_eyes import server
    from agent_eyes.adapters.base import AppInfo

    window = UIElement(
        id=17,
        role="window",
        name="Project",
        bounds=(10, 20, 800, 600),
        platform_ref=object(),
    )
    adapter = MagicMock()
    adapter.list_apps.return_value = [AppInfo(pid=42, name="Demo")]
    adapter.get_browser_trees.return_value = [window]
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "coordinator", coordinator)

    result = asyncio.run(server._handle_window({"action": "list"}))

    line = next(line for line in result.splitlines() if line.startswith("snapshot="))
    token = line.split()[0].removeprefix("snapshot=")
    assert "id=17" in line
    assert "PID 42" in line
    stored = coordinator.observations.resolve(token, 17).value
    assert stored == window
    assert stored is not window
    assert stored.platform_ref is window.platform_ref


def test_app_mutation_rejects_ambiguous_substring(monkeypatch):
    from agent_eyes import server

    first = _RunningApp(activate=True, terminate=True)
    second = _RunningApp(activate=True, terminate=True)
    first.localizedName = lambda: "Demo Stable"
    second.localizedName = lambda: "Demo Beta"
    workspace = SimpleNamespace(runningApplications=lambda: [first, second])
    appkit = ModuleType("AppKit")
    appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: workspace)
    appkit.NSWorkspaceOpenConfiguration = object
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(server.sys, "platform", "darwin")

    result = server._handle_app({"action": "quit", "name": "Demo"})

    assert "AMBIGUOUS_TARGET" in result
    first.terminate.assert_not_called()
    second.terminate.assert_not_called()
