from __future__ import annotations

import asyncio
import inspect
import math

import pytest

from agent_eyes.operation import (
    OperationBudget,
    OperationError,
    OperationErrorCode,
    OperationMode,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_budget_uses_one_absolute_monotonic_deadline():
    clock = FakeClock()
    budget = OperationBudget.start(2.0, clock=clock)

    assert budget.deadline == 102.0
    assert budget.remaining() == 2.0

    clock.advance(0.75)

    assert budget.remaining() == 1.25
    assert budget.deadline == 102.0


def test_checkpoint_fails_with_typed_deadline_error():
    clock = FakeClock()
    budget = OperationBudget.start(0.1, clock=clock)
    clock.advance(0.1)

    with pytest.raises(OperationError) as exc_info:
        budget.checkpoint("native tree")

    assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
    assert "native tree" in str(exc_info.value)


@pytest.mark.parametrize("timeout", [-1.0, math.inf, -math.inf, math.nan])
def test_invalid_timeout_is_rejected(timeout: float):
    with pytest.raises(ValueError):
        OperationBudget.start(timeout)


def test_zero_timeout_is_valid_but_immediately_expired():
    budget = OperationBudget.start(0.0)

    assert budget.remaining() == 0.0
    assert budget.expired is True


def test_wait_for_uses_remaining_budget_and_classifies_timeout():
    async def run() -> None:
        blocker = asyncio.Event()
        budget = OperationBudget.start(0.01)

        with pytest.raises(OperationError) as exc_info:
            await budget.wait_for(blocker.wait(), operation="blocked provider")

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert "blocked provider" in str(exc_info.value)

    asyncio.run(run())


def test_wait_for_preserves_cancellation():
    async def run() -> None:
        blocker = asyncio.Event()
        budget = OperationBudget.start(10.0)
        task = asyncio.create_task(
            budget.wait_for(blocker.wait(), operation="cancelled provider")
        )
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_wait_for_preserves_provider_timeout_error_when_budget_remains():
    async def provider() -> None:
        raise TimeoutError("provider timed out internally")

    async def run() -> None:
        budget = OperationBudget.start(5.0)

        with pytest.raises(TimeoutError, match="provider timed out internally"):
            await budget.wait_for(provider(), operation="provider call")

        assert budget.remaining() > 4.0

    asyncio.run(run())


def test_wait_for_closes_unstarted_coroutine_when_budget_is_already_expired():
    async def provider() -> None:
        return None

    async def run() -> None:
        awaitable = provider()

        with pytest.raises(OperationError) as exc_info:
            await OperationBudget.start(0.0).wait_for(
                awaitable,
                operation="expired provider",
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert inspect.getcoroutinestate(awaitable) == inspect.CORO_CLOSED

    asyncio.run(run())


def test_operation_modes_are_explicit_and_non_ordinal():
    assert OperationMode.FOREGROUND.value == "foreground"
    assert OperationMode.SHADOW.value == "shadow"
    with pytest.raises(TypeError):
        _ = OperationMode.FOREGROUND < OperationMode.SHADOW
