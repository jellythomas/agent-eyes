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


@pytest.mark.parametrize(
    ("parent_timeout", "elapsed", "local_timeout", "expected_deadline"),
    [
        (10.0, 2.0, 3.0, 105.0),
        (10.0, 2.0, 20.0, 110.0),
    ],
)
def test_child_deadline_is_bounded_by_parent_and_local_deadlines(
    parent_timeout: float,
    elapsed: float,
    local_timeout: float,
    expected_deadline: float,
):
    clock = FakeClock()
    parent = OperationBudget.start(parent_timeout, clock=clock)
    clock.advance(elapsed)

    child = parent.child(local_timeout)

    assert child.deadline == expected_deadline


def test_child_shares_the_parent_clock():
    clock = FakeClock()
    parent = OperationBudget.start(10.0, clock=clock)
    child = parent.child(3.0)

    assert child._clock is clock
    assert child.remaining() == 3.0

    clock.advance(1.25)

    assert child.remaining() == 1.75


def test_child_of_expired_parent_remains_expired_at_parent_deadline():
    clock = FakeClock()
    parent = OperationBudget.start(1.0, clock=clock)
    clock.advance(2.0)

    child = parent.child(5.0)

    assert child.deadline == parent.deadline
    assert child.remaining() == 0.0
    assert child.expired is True


def test_zero_length_child_is_valid_but_immediately_expired():
    clock = FakeClock()
    parent = OperationBudget.start(10.0, clock=clock)

    child = parent.child(0.0)

    assert child.deadline == clock.value
    assert child.remaining() == 0.0
    assert child.expired is True


@pytest.mark.parametrize("timeout", [True, False, -1.0, math.inf, -math.inf, math.nan])
def test_child_rejects_invalid_timeout(timeout: float):
    parent = OperationBudget.start(10.0)

    with pytest.raises(
        ValueError,
        match="timeout must be a finite non-negative number",
    ):
        parent.child(timeout)


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


def test_child_wait_for_uses_the_bounded_local_deadline():
    async def run() -> None:
        blocker = asyncio.Event()
        parent = OperationBudget.start(1.0)
        child = parent.child(0.01)

        with pytest.raises(OperationError) as exc_info:
            await child.wait_for(blocker.wait(), operation="bounded child provider")

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert "bounded child provider" in str(exc_info.value)
        assert parent.expired is False

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
