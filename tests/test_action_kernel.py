from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from agent_eyes.action_kernel import (
    ActionDispatchResult,
    ActionKernel,
    ActionOutcome,
    ActionOutcomeStatus,
    ActionPorts,
)
from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode
from agent_eyes.transaction_contract import TransactionOperation


ACTION_OPERATIONS = (
    TransactionOperation.HOVER,
    TransactionOperation.CLICK,
    TransactionOperation.TYPE,
    TransactionOperation.PRESS_KEY,
    TransactionOperation.SCROLL,
)


def _run(coroutine):
    return asyncio.run(coroutine)


def _ready_check(events: list[str], label: str):
    async def check(_budget: OperationBudget) -> bool:
        events.append(label)
        return True

    return check


@pytest.mark.parametrize("operation", ACTION_OPERATIONS)
def test_each_supported_action_preflights_then_dispatches_once(operation):
    async def run() -> None:
        events: list[str] = []
        dispatches = 0

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal dispatches
            dispatches += 1
            events.append("dispatch")
            return ActionDispatchResult.succeeded(changed=True)

        outcome = await ActionKernel().run(
            operation,
            ports=ActionPorts(
                provider_code="native.input",
                capability=_ready_check(events, "capability"),
                focus=_ready_check(events, "focus"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert events == ["capability", "focus", "dispatch"]
        assert dispatches == 1
        assert outcome == ActionOutcome(
            status=ActionOutcomeStatus.SUCCEEDED,
            dispatched=True,
            changed=True,
            retry_safe=False,
            provider_code="native.input",
            error_code=None,
        )

    _run(run())


def test_capability_failure_stops_before_focus_and_dispatch():
    async def run() -> None:
        events: list[str] = []

        async def unavailable(_budget: OperationBudget) -> bool:
            events.append("capability")
            return False

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            events.append("dispatch")
            return ActionDispatchResult.succeeded(changed=True)

        outcome = await ActionKernel().run(
            TransactionOperation.CLICK,
            ports=ActionPorts(
                provider_code="native.ax",
                capability=unavailable,
                focus=_ready_check(events, "focus"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert events == ["capability"]
        assert outcome.status is ActionOutcomeStatus.FAILED
        assert outcome.error_code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert outcome.dispatched is False
        assert outcome.changed is False
        assert outcome.retry_safe is True

    _run(run())


def test_focus_failure_stops_before_dispatch():
    async def run() -> None:
        events: list[str] = []

        async def focus_mismatch(_budget: OperationBudget) -> bool:
            events.append("focus")
            return False

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            events.append("dispatch")
            return ActionDispatchResult.succeeded(changed=True)

        outcome = await ActionKernel().run(
            TransactionOperation.TYPE,
            ports=ActionPorts(
                provider_code="native.input",
                capability=_ready_check(events, "capability"),
                focus=focus_mismatch,
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert events == ["capability", "focus"]
        assert outcome.status is ActionOutcomeStatus.FAILED
        assert outcome.error_code is OperationErrorCode.FOCUS_MISMATCH
        assert outcome.dispatched is False
        assert outcome.retry_safe is True

    _run(run())


def test_focus_preflight_is_optional_for_non_foreground_provider():
    async def run() -> None:
        events: list[str] = []

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            events.append("dispatch")
            return ActionDispatchResult.succeeded(changed=False)

        outcome = await ActionKernel().run(
            TransactionOperation.PRESS_KEY,
            ports=ActionPorts(
                provider_code="cdp-persistent",
                capability=_ready_check(events, "capability"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert events == ["capability", "dispatch"]
        assert outcome.status is ActionOutcomeStatus.SUCCEEDED
        assert outcome.changed is False

    _run(run())


@pytest.mark.parametrize(
    "operation",
    (TransactionOperation.LOCATE, TransactionOperation.EXPECT),
)
def test_non_action_operation_is_rejected_before_any_port(operation):
    async def run() -> None:
        calls = 0

        async def check(_budget: OperationBudget) -> bool:
            nonlocal calls
            calls += 1
            return True

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal calls
            calls += 1
            return ActionDispatchResult.succeeded(changed=False)

        with pytest.raises(ValueError, match="operation must be a supported action"):
            await ActionKernel().run(
                operation,
                ports=ActionPorts(
                    provider_code="native.ax",
                    capability=check,
                    dispatch=dispatch,
                ),
                budget=OperationBudget.start(1.0),
            )

        assert calls == 0

    _run(run())


def test_expired_budget_returns_retryable_failure_without_dispatch():
    async def run() -> None:
        calls = 0

        async def check(_budget: OperationBudget) -> bool:
            nonlocal calls
            calls += 1
            return True

        outcome = await ActionKernel().run(
            TransactionOperation.HOVER,
            ports=ActionPorts(
                provider_code="native.input",
                capability=check,
                dispatch=lambda _budget: pytest.fail("dispatch must not run"),
            ),
            budget=OperationBudget.start(0.0),
        )

        assert calls == 0
        assert outcome.status is ActionOutcomeStatus.FAILED
        assert outcome.error_code is OperationErrorCode.DEADLINE_EXCEEDED
        assert outcome.dispatched is False
        assert outcome.retry_safe is True

    _run(run())


def test_cancellation_during_preflight_propagates_without_dispatch():
    async def run() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        dispatches = 0

        async def capability(_budget: OperationBudget) -> bool:
            started.set()
            await blocker.wait()
            return True

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal dispatches
            dispatches += 1
            return ActionDispatchResult.succeeded(changed=True)

        task = asyncio.create_task(
            ActionKernel().run(
                TransactionOperation.CLICK,
                ports=ActionPorts(
                    provider_code="native.ax",
                    capability=capability,
                    dispatch=dispatch,
                ),
                budget=OperationBudget.start(1.0),
            )
        )
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert dispatches == 0

    _run(run())


def test_cancellation_after_dispatch_becomes_non_retryable_unknown():
    async def run() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        dispatches = 0

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal dispatches
            dispatches += 1
            started.set()
            await blocker.wait()
            return ActionDispatchResult.succeeded(changed=True)

        task = asyncio.create_task(
            ActionKernel().run(
                TransactionOperation.CLICK,
                ports=ActionPorts(
                    provider_code="native.ax",
                    capability=_ready_check([], "capability"),
                    dispatch=dispatch,
                ),
                budget=OperationBudget.start(1.0),
            )
        )
        await started.wait()
        task.cancel()
        outcome = await task

        assert dispatches == 1
        assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
        assert outcome.error_code is OperationErrorCode.OUTCOME_UNKNOWN
        assert outcome.dispatched is True
        assert outcome.changed is False
        assert outcome.retry_safe is False

    _run(run())


def test_deadline_after_dispatch_becomes_non_retryable_unknown():
    async def run() -> None:
        dispatches = 0
        blocker = asyncio.Event()

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal dispatches
            dispatches += 1
            await blocker.wait()
            return ActionDispatchResult.succeeded(changed=True)

        outcome = await ActionKernel().run(
            TransactionOperation.SCROLL,
            ports=ActionPorts(
                provider_code="native.input",
                capability=_ready_check([], "capability"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(0.01),
        )

        assert dispatches == 1
        assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
        assert outcome.error_code is OperationErrorCode.OUTCOME_UNKNOWN
        assert outcome.retry_safe is False

    _run(run())


def test_known_provider_rejection_is_returned_without_fallback_dispatch():
    async def run() -> None:
        dispatches = 0

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            nonlocal dispatches
            dispatches += 1
            return ActionDispatchResult.failed(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                retry_safe=True,
            )

        outcome = await ActionKernel().run(
            TransactionOperation.CLICK,
            ports=ActionPorts(
                provider_code="native.ax",
                capability=_ready_check([], "capability"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert dispatches == 1
        assert outcome.status is ActionOutcomeStatus.FAILED
        assert outcome.dispatched is True
        assert outcome.changed is False
        assert outcome.retry_safe is True
        assert outcome.error_code is OperationErrorCode.UNSUPPORTED_CAPABILITY

    _run(run())


def test_explicit_unknown_dispatch_result_is_never_retryable():
    async def run() -> None:
        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            return ActionDispatchResult.failed(OperationErrorCode.OUTCOME_UNKNOWN)

        outcome = await ActionKernel().run(
            TransactionOperation.PRESS_KEY,
            ports=ActionPorts(
                provider_code="cdp-persistent",
                capability=_ready_check([], "capability"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
        assert outcome.dispatched is True
        assert outcome.retry_safe is False

    _run(run())


def test_typed_text_is_absent_from_outcome_and_logs_when_dispatch_raises(caplog):
    async def run() -> None:
        typed_text = "private inline review comment 72f1e8"

        async def dispatch(_budget: OperationBudget) -> ActionDispatchResult:
            raise RuntimeError(typed_text)

        outcome = await ActionKernel().run(
            TransactionOperation.TYPE,
            ports=ActionPorts(
                provider_code="native.input",
                capability=_ready_check([], "capability"),
                dispatch=dispatch,
            ),
            budget=OperationBudget.start(1.0),
        )

        rendered = f"{outcome!r} {outcome}"
        assert typed_text not in rendered
        assert typed_text not in caplog.text
        assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
        assert outcome.dispatched is True

    _run(run())


def test_preflight_error_message_is_not_reflected_in_outcome():
    async def run() -> None:
        typed_text = "private inline review comment 904ba2"

        async def capability(_budget: OperationBudget) -> bool:
            raise OperationError(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                typed_text,
            )

        outcome = await ActionKernel().run(
            TransactionOperation.TYPE,
            ports=ActionPorts(
                provider_code="native.input",
                capability=capability,
                dispatch=lambda _budget: pytest.fail("dispatch must not run"),
            ),
            budget=OperationBudget.start(1.0),
        )

        assert typed_text not in repr(outcome)
        assert outcome.error_code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert outcome.dispatched is False

    _run(run())


def test_action_outcome_and_dispatch_result_are_frozen():
    outcome = ActionOutcome(
        status=ActionOutcomeStatus.SUCCEEDED,
        dispatched=True,
        changed=True,
        retry_safe=False,
        provider_code="native.ax",
        error_code=None,
    )
    result = ActionDispatchResult.succeeded(changed=True)

    with pytest.raises(FrozenInstanceError):
        outcome.changed = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.changed = False  # type: ignore[misc]


def test_provider_code_validation_never_reflects_the_supplied_value():
    provider_code = "private typed content must not become provider metadata"

    with pytest.raises(ValueError) as exc_info:
        ActionPorts(
            provider_code=provider_code,
            capability=lambda _budget: pytest.fail("unused"),
            dispatch=lambda _budget: pytest.fail("unused"),
        )

    assert provider_code not in str(exc_info.value)


@pytest.mark.parametrize(
    "factory",
    (
        lambda secret: ActionDispatchResult(
            acknowledged=False,
            changed=False,
            error_code=secret,
        ),
        lambda secret: ActionOutcome(
            status=ActionOutcomeStatus.FAILED,
            dispatched=False,
            changed=False,
            retry_safe=True,
            provider_code="native",
            error_code=secret,
        ),
    ),
)
def test_error_code_rejects_text_without_reflecting_it(factory):
    typed_text = "private inline review comment 293af0"

    with pytest.raises(ValueError) as exc_info:
        factory(typed_text)

    assert typed_text not in str(exc_info.value)


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ActionDispatchResult(acknowledged=True, changed=True, retry_safe=True),
        lambda: ActionDispatchResult(
            acknowledged=True,
            changed=True,
            error_code=OperationErrorCode.FOCUS_MISMATCH,
        ),
        lambda: ActionDispatchResult(acknowledged=False, changed=True),
        lambda: ActionDispatchResult(acknowledged=False, changed=False),
    ),
)
def test_dispatch_result_rejects_inconsistent_states(factory):
    with pytest.raises(ValueError):
        factory()
