"""Allowlisted, content-free telemetry for transaction fast-path calls.

The public trace type intentionally has no free-form string or mapping fields.  A
sink receives only the frozen trace, never tool arguments, UI content, results, or
exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import TypeAlias

from .operation import OperationErrorCode


class TelemetryTool(str, Enum):
    """Transaction tools that may emit a trace."""

    OBSERVE_TARGET = "observe_target"
    EXECUTE = "execute"


class TelemetryResultCode(str, Enum):
    """Stable non-provider outcomes that are not operation errors."""

    SUCCESS = "SUCCESS"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    CANCELLED = "CANCELLED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class TelemetryCacheState(str, Enum):
    """Bounded inventory and single-flight state."""

    NOT_APPLICABLE = "not_applicable"
    BYPASS = "bypass"
    MISS = "miss"
    SHARED = "shared"
    HIT = "hit"


class TelemetryPhase(str, Enum):
    """Closed set of request phases that may contribute durations."""

    QUEUE = "queue"
    RESOLUTION = "resolution"
    ACTIVATION = "activation"
    OBSERVATION = "observation"
    ACTION = "action"
    WAIT = "wait"


TraceResultCode: TypeAlias = TelemetryResultCode | OperationErrorCode


_DURATION_FIELDS = (
    "total_ms",
    "queue_ms",
    "resolution_ms",
    "activation_ms",
    "observation_ms",
    "action_ms",
    "wait_ms",
)

_COUNT_FIELDS = (
    "provider_scans",
    "nodes_scanned",
    "completed_steps",
    "original_output_bytes",
    "returned_output_bytes",
)


@dataclass(frozen=True, slots=True)
class TransactionTrace:
    """One immutable trace containing only numeric and enumerated metadata."""

    tool: TelemetryTool
    result_code: TraceResultCode
    total_ms: int | float
    queue_ms: int | float = 0.0
    resolution_ms: int | float = 0.0
    activation_ms: int | float = 0.0
    observation_ms: int | float = 0.0
    action_ms: int | float = 0.0
    wait_ms: int | float = 0.0
    provider_scans: int = 0
    nodes_scanned: int = 0
    cache_state: TelemetryCacheState = TelemetryCacheState.NOT_APPLICABLE
    completed_steps: int = 0
    original_output_bytes: int = 0
    returned_output_bytes: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool, TelemetryTool):
            raise ValueError("tool must be a TelemetryTool")
        if not isinstance(
            self.result_code,
            (TelemetryResultCode, OperationErrorCode),
        ):
            raise ValueError("result_code must be a stable result-code enum")
        if not isinstance(self.cache_state, TelemetryCacheState):
            raise ValueError("cache_state must be a TelemetryCacheState")
        if not isinstance(self.truncated, bool):
            raise ValueError("truncated must be a boolean")

        for field_name in _DURATION_FIELDS:
            _validate_duration(getattr(self, field_name), name=field_name)
        for field_name in _COUNT_FIELDS:
            _validate_count(getattr(self, field_name), name=field_name)

    def to_dict(self) -> dict[str, str | int | float | bool]:
        """Return the complete primitive allowlist for a structured sink."""
        return {
            "tool": self.tool.value,
            "result_code": self.result_code.value,
            "total_ms": self.total_ms,
            "queue_ms": self.queue_ms,
            "resolution_ms": self.resolution_ms,
            "activation_ms": self.activation_ms,
            "observation_ms": self.observation_ms,
            "action_ms": self.action_ms,
            "wait_ms": self.wait_ms,
            "provider_scans": self.provider_scans,
            "nodes_scanned": self.nodes_scanned,
            "cache_state": self.cache_state.value,
            "completed_steps": self.completed_steps,
            "original_output_bytes": self.original_output_bytes,
            "returned_output_bytes": self.returned_output_bytes,
            "truncated": self.truncated,
        }


_clock: Callable[[], float] = time.monotonic


@dataclass(slots=True)
class TransactionTelemetryRecorder:
    """Request-local numeric and enumerated telemetry under one terminal trace."""

    tool: TelemetryTool
    queue_ms: int | float = 0.0
    resolution_ms: int | float = 0.0
    activation_ms: int | float = 0.0
    observation_ms: int | float = 0.0
    action_ms: int | float = 0.0
    wait_ms: int | float = 0.0
    provider_scans: int = 0
    nodes_scanned: int = 0
    cache_state: TelemetryCacheState = TelemetryCacheState.NOT_APPLICABLE
    _finished: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool, TelemetryTool):
            raise ValueError("tool must be a TelemetryTool")

    def start_phase(self) -> float:
        """Capture one monotonic numeric phase boundary."""
        started_at = _clock()
        _validate_duration(started_at, name="phase start")
        return started_at

    def finish_phase(
        self,
        phase: TelemetryPhase,
        started_at: int | float,
        *,
        exclude_ms: int | float = 0.0,
    ) -> None:
        """Accumulate one phase while excluding explicitly nested phase time."""
        if not isinstance(phase, TelemetryPhase):
            raise ValueError("phase must be a TelemetryPhase")
        _validate_duration(started_at, name="phase start")
        _validate_duration(exclude_ms, name="excluded duration")
        elapsed_ms = max(0.0, (_clock() - started_at) * 1_000 - exclude_ms)
        if phase is TelemetryPhase.QUEUE:
            self.queue_ms += elapsed_ms
        elif phase is TelemetryPhase.RESOLUTION:
            self.resolution_ms += elapsed_ms
        elif phase is TelemetryPhase.ACTIVATION:
            self.activation_ms += elapsed_ms
        elif phase is TelemetryPhase.OBSERVATION:
            self.observation_ms += elapsed_ms
        elif phase is TelemetryPhase.ACTION:
            self.action_ms += elapsed_ms
        else:
            self.wait_ms += elapsed_ms

    def phase_ms(self, phase: TelemetryPhase) -> int | float:
        """Return one numeric phase total for nested-duration exclusion."""
        if not isinstance(phase, TelemetryPhase):
            raise ValueError("phase must be a TelemetryPhase")
        if phase is TelemetryPhase.QUEUE:
            return self.queue_ms
        if phase is TelemetryPhase.RESOLUTION:
            return self.resolution_ms
        if phase is TelemetryPhase.ACTIVATION:
            return self.activation_ms
        if phase is TelemetryPhase.OBSERVATION:
            return self.observation_ms
        if phase is TelemetryPhase.ACTION:
            return self.action_ms
        return self.wait_ms

    def record_provider_scan(self, nodes: int) -> None:
        """Record one bounded provider observation and its indexed node count."""
        _validate_count(nodes, name="nodes")
        self.provider_scans += 1
        self.nodes_scanned += nodes

    def record_cache_state(self, cache_state: TelemetryCacheState) -> None:
        """Record only the stable inventory cache enum returned by resolution."""
        if not isinstance(cache_state, TelemetryCacheState):
            raise ValueError("cache_state must be a TelemetryCacheState")
        self.cache_state = cache_state

    def finish_trace(
        self,
        *,
        result_code: TraceResultCode,
        total_ms: int | float,
        completed_steps: int,
        original_output_bytes: int,
        returned_output_bytes: int,
        truncated: bool,
    ) -> TransactionTrace:
        """Freeze the request state into its single terminal trace."""
        if self._finished:
            raise RuntimeError("transaction telemetry is already finished")
        self._finished = True
        return TransactionTrace(
            tool=self.tool,
            result_code=result_code,
            total_ms=total_ms,
            queue_ms=self.queue_ms,
            resolution_ms=self.resolution_ms,
            activation_ms=self.activation_ms,
            observation_ms=self.observation_ms,
            action_ms=self.action_ms,
            wait_ms=self.wait_ms,
            provider_scans=self.provider_scans,
            nodes_scanned=self.nodes_scanned,
            cache_state=self.cache_state,
            completed_steps=completed_steps,
            original_output_bytes=original_output_bytes,
            returned_output_bytes=returned_output_bytes,
            truncated=truncated,
        )


_current_transaction_telemetry: ContextVar[TransactionTelemetryRecorder | None] = (
    ContextVar("agent_eyes_transaction_telemetry", default=None)
)


def begin_transaction_telemetry(
    tool: TelemetryTool,
) -> tuple[TransactionTelemetryRecorder, Token]:
    """Install one request recorder and return its exact reset token."""
    recorder = TransactionTelemetryRecorder(tool=tool)
    return recorder, _current_transaction_telemetry.set(recorder)


def current_transaction_telemetry() -> TransactionTelemetryRecorder | None:
    """Return only the recorder inherited by the current async request context."""
    return _current_transaction_telemetry.get()


def reset_transaction_telemetry(token: Token) -> None:
    """Restore the prior request context after terminal trace emission."""
    if not isinstance(token, Token):
        raise TypeError("telemetry reset requires a context token")
    _current_transaction_telemetry.reset(token)


TraceSink: TypeAlias = Callable[[TransactionTrace], None]


class TelemetryEmitter:
    """Deliver a safe trace without allowing sink failures into tool results."""

    __slots__ = ("_sink",)

    def __init__(self, sink: TraceSink | None = None) -> None:
        if sink is not None and not callable(sink):
            raise TypeError("telemetry sink must be callable")
        self._sink = sink

    def emit(self, trace: TransactionTrace) -> None:
        if not isinstance(trace, TransactionTrace):
            raise TypeError("telemetry payload must be a TransactionTrace")
        sink = self._sink
        if sink is None:
            return
        try:
            sink(trace)
        except (Exception, asyncio.CancelledError):
            # Telemetry is observational. Its failure must not replace an already
            # determined tool outcome or write diagnostics to MCP stdout.
            return


def _validate_duration(value: object, *, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")


def _validate_count(value: object, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
