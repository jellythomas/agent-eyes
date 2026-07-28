from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields
import json
import math
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent
import pytest

from agent_eyes.action_kernel import ActionDispatchResult, ActionPorts
from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.locators import LocatorIndex
from agent_eyes.operation import OperationErrorCode
from agent_eyes.target_resolver import (
    InventoryCacheStatus,
    ResolvedTarget,
    ResolutionSource,
    TargetResolution,
)
from agent_eyes.telemetry import (
    TelemetryCacheState,
    TelemetryEmitter,
    TelemetryPhase,
    TelemetryResultCode,
    TelemetryTool,
    TransactionTelemetryRecorder,
    TransactionTrace,
    current_transaction_telemetry,
)
from agent_eyes.transaction_contract import TargetMode
from agent_eyes.transactions import TransactionPorts, TransactionTarget, TransactionView


SENTINELS = {
    "url": "https://bitbucket.example/private/pull-requests/42",
    "query": "confidential pr title",
    "title": "Confidential PR title",
    "selector": "button[name='Post secret']",
    "alias": "secret_editor_alias",
    "text": "do not record this inline comment",
    "page": "private rendered page contents",
    "file": "/Users/example/private/comment.txt",
    "arguments": {"text": "secret raw arguments"},
    "result": "secret raw result",
    "exception": RuntimeError("secret exception message"),
}


class _StepClock:
    def __init__(self, step_seconds: float = 0.001) -> None:
        self._step_seconds = step_seconds
        self._current = 0.0

    def __call__(self) -> float:
        current = self._current
        self._current += self._step_seconds
        return current


class _InlineWorker:
    async def run(self, call, **_kwargs):
        return call()

    async def wait_until_idle(self) -> None:
        return None


class _InstrumentedTransactionRuntime:
    def __init__(self, _request) -> None:
        root = UIElement(
            id=1,
            role="window",
            name=SENTINELS["page"],
            platform_ref=object(),
            children=[
                UIElement(
                    id=2,
                    role="button",
                    name="Apply",
                    actions=["press"],
                    platform_ref=object(),
                )
            ],
        )
        self._view = TransactionView(index=LocatorIndex.from_roots(root))

    def ports(self) -> TransactionPorts:
        async def resolve(_target, _activate, _budget) -> TransactionTarget:
            resolution = TargetResolution(
                target=ResolvedTarget(
                    mode=TargetMode.FOREGROUND,
                    target_id="pid:73",
                    pid=73,
                    source=ResolutionSource.PID,
                ),
                cache_status=InventoryCacheStatus.BYPASS,
                activated=True,
            )
            return TransactionTarget(target_id="pid:73", value=resolution)

        async def observe(_target, _budget) -> TransactionView:
            return self._view

        async def refresh(_target, _current, _locator, _budget) -> TransactionView:
            raise AssertionError("one-view fixture must not refresh")

        def action_ports(_step, _element, _target) -> ActionPorts:
            async def capability(_budget) -> bool:
                return True

            async def focus(_budget) -> bool:
                return True

            async def dispatch(_budget) -> ActionDispatchResult:
                return ActionDispatchResult.succeeded(changed=True)

            return ActionPorts(
                provider_code="test.native",
                capability=capability,
                focus=focus,
                dispatch=dispatch,
            )

        return TransactionPorts(
            resolve=resolve,
            observe=observe,
            refresh=refresh,
            action_ports=action_ports,
        )


class _BrowserObservationAdapter:
    def __init__(self, window: UIElement) -> None:
        self._window = window

    def get_subtree(self, element: UIElement, max_depth: int = 10) -> UIElement:
        assert element is self._window
        assert max_depth == 10
        return UIElement(
            id=10,
            role="document",
            name=SENTINELS["page"],
            platform_ref=object(),
            children=[
                UIElement(
                    id=11,
                    role="heading",
                    name="Pull request 42",
                    platform_ref=object(),
                )
            ],
        )

    def is_element_selected(self, _element: UIElement) -> bool:
        return True

    def is_window_focused(self, window: UIElement) -> bool:
        return window is self._window


def _trace(**overrides: object) -> TransactionTrace:
    values: dict[str, object] = {
        "tool": TelemetryTool.EXECUTE,
        "result_code": TelemetryResultCode.SUCCESS,
        "total_ms": 81.5,
        "queue_ms": 1.5,
        "resolution_ms": 10.0,
        "activation_ms": 3.0,
        "observation_ms": 20.0,
        "action_ms": 28.0,
        "wait_ms": 19.0,
        "provider_scans": 1,
        "nodes_scanned": 48,
        "cache_state": TelemetryCacheState.MISS,
        "completed_steps": 6,
        "original_output_bytes": 384,
        "returned_output_bytes": 384,
        "truncated": False,
    }
    values.update(overrides)
    return TransactionTrace(**values)


def _serialized(trace: TransactionTrace) -> str:
    return json.dumps(trace.to_dict(), sort_keys=True)


def _assert_sentinels_absent(trace: TransactionTrace) -> None:
    serialized = _serialized(trace)
    for sentinel in SENTINELS.values():
        if isinstance(sentinel, str):
            assert sentinel not in serialized


def _assert_safe_terminal_schema(trace: TransactionTrace) -> None:
    payload = trace.to_dict()
    assert set(payload) == {field.name for field in fields(TransactionTrace)}
    assert all(isinstance(value, (str, int, float, bool)) for value in payload.values())
    _assert_sentinels_absent(trace)


def test_success_trace_is_frozen_and_contains_only_allowlisted_fields() -> None:
    trace = _trace()

    assert {field.name for field in fields(trace)} == {
        "tool",
        "result_code",
        "total_ms",
        "queue_ms",
        "resolution_ms",
        "activation_ms",
        "observation_ms",
        "action_ms",
        "wait_ms",
        "provider_scans",
        "nodes_scanned",
        "cache_state",
        "completed_steps",
        "original_output_bytes",
        "returned_output_bytes",
        "truncated",
    }
    assert trace.to_dict() == {
        "tool": "execute",
        "result_code": "SUCCESS",
        "total_ms": 81.5,
        "queue_ms": 1.5,
        "resolution_ms": 10.0,
        "activation_ms": 3.0,
        "observation_ms": 20.0,
        "action_ms": 28.0,
        "wait_ms": 19.0,
        "provider_scans": 1,
        "nodes_scanned": 48,
        "cache_state": "miss",
        "completed_steps": 6,
        "original_output_bytes": 384,
        "returned_output_bytes": 384,
        "truncated": False,
    }
    with pytest.raises(FrozenInstanceError):
        trace.completed_steps = 7  # type: ignore[misc]
    assert not hasattr(trace, "__dict__")
    _assert_sentinels_absent(trace)


def test_request_recorder_contains_only_numeric_enumerated_and_terminal_state() -> None:
    recorder = TransactionTelemetryRecorder(tool=TelemetryTool.EXECUTE)

    assert {field.name for field in fields(recorder)} == {
        "tool",
        "queue_ms",
        "resolution_ms",
        "activation_ms",
        "observation_ms",
        "action_ms",
        "wait_ms",
        "provider_scans",
        "nodes_scanned",
        "cache_state",
        "_finished",
    }
    assert not hasattr(recorder, "__dict__")
    assert all(
        isinstance(value, (int, float, TelemetryTool, TelemetryCacheState))
        for field in fields(recorder)
        if (value := getattr(recorder, field.name)) is not None
    )


@pytest.mark.parametrize(("field_name", "sentinel"), SENTINELS.items())
def test_request_recorder_rejects_every_forbidden_content_field(
    field_name: str,
    sentinel: object,
) -> None:
    with pytest.raises(TypeError):
        TransactionTelemetryRecorder(
            tool=TelemetryTool.EXECUTE,
            **{field_name: sentinel},
        )


@pytest.mark.parametrize(("field_name", "sentinel"), SENTINELS.items())
def test_trace_constructor_rejects_every_forbidden_data_field(
    field_name: str,
    sentinel: object,
) -> None:
    with pytest.raises(TypeError):
        _trace(**{field_name: sentinel})


def test_failure_trace_accepts_only_a_stable_error_enum_without_exception_detail() -> (
    None
):
    trace = _trace(
        result_code=OperationErrorCode.AMBIGUOUS_ELEMENT,
        completed_steps=2,
        action_ms=0.0,
        wait_ms=0.0,
        original_output_bytes=96,
        returned_output_bytes=96,
    )

    serialized = _serialized(trace)
    assert trace.to_dict()["result_code"] == "AMBIGUOUS_ELEMENT"
    assert "exception" not in serialized
    assert "message" not in serialized
    _assert_sentinels_absent(trace)


def test_truncation_trace_records_only_byte_counts_and_flag() -> None:
    trace = _trace(
        tool=TelemetryTool.OBSERVE_TARGET,
        original_output_bytes=12_000,
        returned_output_bytes=4_096,
        truncated=True,
        completed_steps=0,
        cache_state=TelemetryCacheState.HIT,
    )

    payload = trace.to_dict()
    assert payload["tool"] == "observe_target"
    assert payload["original_output_bytes"] == 12_000
    assert payload["returned_output_bytes"] == 4_096
    assert payload["truncated"] is True
    assert set(payload) == {field.name for field in fields(trace)}
    _assert_sentinels_absent(trace)


def test_emitter_delivers_exactly_the_frozen_trace_to_an_injected_sink() -> None:
    traces: list[TransactionTrace] = []
    trace = _trace()

    TelemetryEmitter(traces.append).emit(trace)

    assert traces == [trace]
    assert traces[0] is trace


def test_broken_sink_is_failure_isolated_and_cannot_replace_tool_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts: list[TransactionTrace] = []

    def broken_sink(trace: TransactionTrace) -> None:
        attempts.append(trace)
        raise RuntimeError("sink failed with secret exception detail")

    expected_tool_outcome = {"status": "ok", "completed_steps": 6}
    actual_tool_outcome = expected_tool_outcome

    result = TelemetryEmitter(broken_sink).emit(_trace())

    assert result is None
    assert actual_tool_outcome is expected_tool_outcome
    assert len(attempts) == 1
    captured = capsys.readouterr()
    assert "secret exception detail" not in captured.out
    assert "secret exception detail" not in captured.err


def test_sink_cancelled_error_is_isolated_from_the_completed_tool_outcome() -> None:
    attempts = 0

    def cancelled_sink(_trace: TransactionTrace) -> None:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    assert TelemetryEmitter(cancelled_sink).emit(_trace()) is None
    assert attempts == 1


def test_default_emitter_is_a_silent_noop(capsys: pytest.CaptureFixture[str]) -> None:
    assert TelemetryEmitter().emit(_trace()) is None

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "value",
    [-1, -0.1, math.inf, -math.inf, math.nan, True, "1", None],
)
def test_durations_reject_non_finite_non_numeric_and_negative_values(
    value: object,
) -> None:
    with pytest.raises(ValueError):
        _trace(total_ms=value)


@pytest.mark.parametrize(
    "field_name",
    [
        "provider_scans",
        "nodes_scanned",
        "completed_steps",
        "original_output_bytes",
        "returned_output_bytes",
    ],
)
@pytest.mark.parametrize("value", [-1, 1.5, True, "1", None])
def test_counts_require_non_negative_integers(field_name: str, value: object) -> None:
    with pytest.raises(ValueError):
        _trace(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tool", "execute"),
        ("result_code", "SUCCESS"),
        ("cache_state", "miss"),
        ("truncated", 1),
    ],
)
def test_enumerated_and_boolean_fields_reject_lookalike_primitive_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        _trace(**{field_name: value})


def test_emitter_rejects_non_callable_sinks_and_non_trace_payloads() -> None:
    with pytest.raises(TypeError):
        TelemetryEmitter(object())  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        TelemetryEmitter(lambda _trace: None).emit(SENTINELS["arguments"])  # type: ignore[arg-type]


def test_server_emits_exactly_one_success_trace_without_raw_call_data(
    monkeypatch,
) -> None:
    from agent_eyes import server

    traces: list[TransactionTrace] = []
    secret = SENTINELS["text"]

    async def core(name: str, arguments: dict):
        assert name == "execute"
        assert arguments["text"] == secret
        return [
            TextContent(
                type="text",
                text=('{"status":"succeeded","completed_steps":6,"elapsed_ms":4}'),
            )
        ]

    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server, "_transaction_telemetry", TelemetryEmitter(traces.append)
    )

    result = asyncio.run(server.call_tool("execute", {"text": secret}))

    assert result[0].text.startswith('{"status":"succeeded"')
    assert len(traces) == 1
    assert traces[0].tool is TelemetryTool.EXECUTE
    assert traces[0].result_code is TelemetryResultCode.SUCCESS
    assert traces[0].completed_steps == 6
    assert traces[0].returned_output_bytes == len(result[0].text.encode())
    _assert_safe_terminal_schema(traces[0])
    assert current_transaction_telemetry() is None


def test_server_error_trace_uses_stable_code_and_bounded_output_metadata(
    monkeypatch,
) -> None:
    from agent_eyes import server

    traces: list[TransactionTrace] = []
    rendered = (
        'ERROR: {"status":"failed","completed_steps":2,"code":"AMBIGUOUS_ELEMENT"}'
    )

    async def core(_name: str, _arguments: dict):
        return CallToolResult(
            content=[TextContent(type="text", text=rendered)],
            isError=True,
        )

    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server, "_transaction_telemetry", TelemetryEmitter(traces.append)
    )

    result = asyncio.run(server.call_tool("execute", SENTINELS["arguments"]))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert len(traces) == 1
    assert traces[0].result_code is OperationErrorCode.AMBIGUOUS_ELEMENT
    assert traces[0].completed_steps == 2
    assert traces[0].original_output_bytes == len(rendered.encode())
    _assert_safe_terminal_schema(traces[0])
    assert current_transaction_telemetry() is None


def test_server_deadline_trace_is_exactly_once_safe_and_context_is_reset(
    monkeypatch,
) -> None:
    from agent_eyes import server

    traces: list[TransactionTrace] = []

    async def core(_name: str, _arguments: dict):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="ERROR: DEADLINE_EXCEEDED: bounded operation expired",
                )
            ],
            isError=True,
        )

    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server,
        "_transaction_telemetry",
        TelemetryEmitter(traces.append),
    )

    result = asyncio.run(server.call_tool("execute", SENTINELS["arguments"]))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert len(traces) == 1
    assert traces[0].result_code is OperationErrorCode.DEADLINE_EXCEEDED
    _assert_safe_terminal_schema(traces[0])
    assert current_transaction_telemetry() is None


def test_server_cancellation_still_emits_one_trace_and_propagates(
    monkeypatch,
) -> None:
    from agent_eyes import server

    traces: list[TransactionTrace] = []

    async def core(_name: str, _arguments: dict):
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server, "_transaction_telemetry", TelemetryEmitter(traces.append)
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.call_tool("observe_target", SENTINELS["arguments"]))

    assert len(traces) == 1
    assert traces[0].tool is TelemetryTool.OBSERVE_TARGET
    assert traces[0].result_code is TelemetryResultCode.CANCELLED
    assert traces[0].returned_output_bytes == 0
    _assert_safe_terminal_schema(traces[0])
    assert current_transaction_telemetry() is None


def test_real_semantic_validation_failure_traces_once_before_readiness(
    monkeypatch,
) -> None:
    from agent_eyes import server

    traces: list[TransactionTrace] = []

    async def readiness_must_not_run():
        raise AssertionError("semantic validation must precede readiness")

    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness_must_not_run)
    monkeypatch.setattr(
        server, "_transaction_telemetry", TelemetryEmitter(traces.append)
    )

    result = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {"pid": 73},
                "steps": [
                    {
                        "op": "type",
                        "ref": "missing_alias",
                        "text": SENTINELS["text"],
                    }
                ],
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert SENTINELS["text"] not in result.content[0].text
    assert len(traces) == 1
    assert traces[0].result_code is OperationErrorCode.INVALID_TRANSACTION
    assert traces[0].completed_steps == 0
    _assert_safe_terminal_schema(traces[0])
    assert current_transaction_telemetry() is None


def test_server_sink_failure_attempts_one_safe_trace_and_preserves_result(
    monkeypatch,
) -> None:
    from agent_eyes import server

    attempts: list[TransactionTrace] = []

    async def core(_name: str, _arguments: dict):
        return [
            TextContent(
                type="text",
                text='{"status":"succeeded","completed_steps":1}',
            )
        ]

    def broken_sink(trace: TransactionTrace) -> None:
        attempts.append(trace)
        raise RuntimeError("private sink failure detail")

    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server,
        "_transaction_telemetry",
        TelemetryEmitter(broken_sink),
    )

    result = asyncio.run(server.call_tool("execute", SENTINELS["arguments"]))

    assert not isinstance(result, CallToolResult)
    assert result[0].text.startswith('{"status":"succeeded"')
    assert len(attempts) == 1
    _assert_safe_terminal_schema(attempts[0])
    assert current_transaction_telemetry() is None


def test_execute_trace_records_non_zero_request_phases_and_exact_scan_counts(
    monkeypatch,
) -> None:
    from agent_eyes import server, telemetry

    traces: list[TransactionTrace] = []
    monkeypatch.setattr(telemetry, "_clock", _StepClock())
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(
        server,
        "_ForegroundTransactionRuntime",
        _InstrumentedTransactionRuntime,
    )
    monkeypatch.setattr(
        server,
        "_transaction_telemetry",
        TelemetryEmitter(traces.append),
    )

    result = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {"pid": 73},
                "steps": [
                    {
                        "op": "locate",
                        "as": "apply",
                        "role": "button",
                        "name": "Apply",
                    },
                    {"op": "click", "ref": "apply"},
                ],
            },
        )
    )

    assert not isinstance(result, CallToolResult)
    assert len(traces) == 1
    trace = traces[0]
    assert trace.result_code is TelemetryResultCode.SUCCESS
    assert trace.queue_ms == pytest.approx(1.0)
    assert trace.resolution_ms == pytest.approx(1.0)
    assert trace.observation_ms == pytest.approx(1.0)
    assert trace.action_ms == pytest.approx(1.0)
    assert trace.wait_ms == pytest.approx(2.0)
    assert trace.activation_ms == 0
    assert trace.provider_scans == 1
    assert trace.nodes_scanned == 2
    assert trace.cache_state is TelemetryCacheState.BYPASS
    assert trace.completed_steps == 2
    _assert_sentinels_absent(trace)


def test_native_completion_checks_record_every_scoped_provider_scan(
    monkeypatch,
) -> None:
    from agent_eyes import server
    from agent_eyes.native_events import NativeActionResult
    from agent_eyes.telemetry import (
        TelemetryTool,
        begin_transaction_telemetry,
        reset_transaction_telemetry,
    )

    checks = iter(((False, 7), (False, 5), (True, 3)))

    async def run_native_action_until(_pid, _call, condition, **_kwargs):
        assert condition() is False
        assert condition() is False
        assert condition() is True
        return NativeActionResult(
            action_result=True,
            condition_met=True,
            elapsed=0.001,
            event_driven=True,
            checks=3,
        )

    monkeypatch.setattr(server, "run_native_action_until", run_native_action_until)
    recorder, token = begin_transaction_telemetry(TelemetryTool.EXECUTE)
    try:
        result = asyncio.run(
            server._run_transaction_mutation_until(
                lambda: True,
                lambda: next(checks),
                pid=73,
                budget=server.OperationBudget.start(1.0),
                operation="test completion",
                action_worker=SimpleNamespace(),
            )
        )
    finally:
        reset_transaction_telemetry(token)

    assert result is True
    assert recorder.provider_scans == 3
    assert recorder.nodes_scanned == 15


def test_observe_target_trace_separates_activation_resolution_and_observation(
    monkeypatch,
) -> None:
    from agent_eyes import server, telemetry

    traces: list[TransactionTrace] = []
    window = UIElement(
        id=90,
        role="window",
        name=SENTINELS["title"],
        platform_ref=object(),
    )
    target = BrowserTarget(
        browser="Firefox",
        pid=41,
        title=SENTINELS["title"],
        url=SENTINELS["url"],
        window_index=2,
        tab_index=4,
        element=UIElement(
            id=91,
            role="tab",
            name=SENTINELS["title"],
            platform_ref=object(),
        ),
        window_element=window,
    )
    activation_calls = 0

    def inventory(_adapter, *, require_complete: bool = False):
        assert require_complete is False
        return [target]

    async def activate(selected, timeout: float = 0.75, *, budget=None):
        nonlocal activation_calls
        assert selected.identifier == target.identifier
        assert timeout > 0
        assert budget is None
        activation_calls += 1
        return True

    monkeypatch.setattr(telemetry, "_clock", _StepClock())
    monkeypatch.setattr(server, "native_adapter", _BrowserObservationAdapter(window))
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "input_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", inventory)
    monkeypatch.setattr(server, "_activate_browser_target_and_wait", activate)
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(
        server,
        "_transaction_telemetry",
        TelemetryEmitter(traces.append),
    )

    result = asyncio.run(
        server.call_tool(
            "observe_target",
            {
                "query": SENTINELS["query"],
                "intent": "interact",
                "selectors": [{"role": "heading", "name": "Pull request 42"}],
            },
        )
    )

    assert not isinstance(result, CallToolResult)
    assert activation_calls == 1
    assert len(traces) == 1
    trace = traces[0]
    assert trace.result_code is TelemetryResultCode.SUCCESS
    assert trace.queue_ms == pytest.approx(1.0)
    assert trace.resolution_ms == pytest.approx(2.0)
    assert trace.activation_ms == pytest.approx(1.0)
    assert trace.observation_ms == pytest.approx(2.0)
    assert trace.action_ms == 0
    assert trace.wait_ms == 0
    assert trace.provider_scans == 2
    assert trace.nodes_scanned == 2
    assert trace.cache_state is TelemetryCacheState.MISS
    _assert_sentinels_absent(trace)


def test_concurrent_transaction_calls_keep_request_telemetry_isolated(
    monkeypatch,
) -> None:
    from agent_eyes import server, telemetry

    traces: list[TransactionTrace] = []
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def core(name: str, _arguments: dict):
        nonlocal started
        recorder = current_transaction_telemetry()
        assert recorder is not None
        phase_started = recorder.start_phase()
        recorder.record_provider_scan(3 if name == "execute" else 7)
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        await release.wait()
        recorder.finish_phase(TelemetryPhase.OBSERVATION, phase_started)
        return [
            TextContent(
                type="text",
                text='{"status":"succeeded","completed_steps":1}',
            )
        ]

    async def run() -> None:
        execute = asyncio.create_task(
            server.call_tool("execute", SENTINELS["arguments"])
        )
        observe = asyncio.create_task(
            server.call_tool("observe_target", SENTINELS["arguments"])
        )
        await both_started.wait()
        release.set()
        await asyncio.gather(execute, observe)

    monkeypatch.setattr(telemetry, "_clock", _StepClock())
    monkeypatch.setattr(server, "_call_tool_core", core)
    monkeypatch.setattr(
        server,
        "_transaction_telemetry",
        TelemetryEmitter(traces.append),
    )

    asyncio.run(run())

    assert len(traces) == 2
    by_tool = {trace.tool: trace for trace in traces}
    assert by_tool[TelemetryTool.EXECUTE].provider_scans == 1
    assert by_tool[TelemetryTool.EXECUTE].nodes_scanned == 3
    assert by_tool[TelemetryTool.OBSERVE_TARGET].provider_scans == 1
    assert by_tool[TelemetryTool.OBSERVE_TARGET].nodes_scanned == 7
    assert all(trace.observation_ms > 0 for trace in traces)
    for trace in traces:
        _assert_sentinels_absent(trace)
    assert current_transaction_telemetry() is None
