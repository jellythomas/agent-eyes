"""Provider-neutral, one-dispatch action execution for UI transactions.

The kernel deliberately receives already-selected callables instead of adapters,
elements, coordinates, keys, or typed text.  Provider integrations own those values
inside closures and return only structured, user-safe state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import re
from typing import Awaitable, Protocol

from .operation import OperationBudget, OperationError, OperationErrorCode
from .transaction_contract import TransactionOperation


_ACTION_OPERATIONS = frozenset(
    {
        TransactionOperation.HOVER,
        TransactionOperation.CLICK,
        TransactionOperation.TYPE,
        TransactionOperation.PRESS_KEY,
        TransactionOperation.SCROLL,
    }
)
_PROVIDER_CODE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")


class ActionOutcomeStatus(str, Enum):
    """Stable state of one selected action."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class ActionDispatchResult:
    """A provider's value-only acknowledgement of its one selected action."""

    acknowledged: bool
    changed: bool
    retry_safe: bool = False
    error_code: OperationErrorCode | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (self.acknowledged, self.changed, self.retry_safe)
        ):
            raise ValueError("dispatch result flags must be booleans")
        if self.error_code is not None and not isinstance(
            self.error_code, OperationErrorCode
        ):
            raise ValueError("dispatch error_code must be an OperationErrorCode")
        if self.acknowledged:
            if self.error_code is not None or self.retry_safe:
                raise ValueError("successful dispatch result is inconsistent")
            return
        if self.changed or self.error_code is None:
            raise ValueError("failed dispatch result is inconsistent")
        if self.error_code is OperationErrorCode.OUTCOME_UNKNOWN and self.retry_safe:
            raise ValueError("unknown dispatch result cannot be retryable")

    @classmethod
    def succeeded(cls, *, changed: bool) -> ActionDispatchResult:
        return cls(acknowledged=True, changed=changed)

    @classmethod
    def failed(
        cls,
        error_code: OperationErrorCode,
        *,
        retry_safe: bool = False,
    ) -> ActionDispatchResult:
        return cls(
            acknowledged=False,
            changed=False,
            retry_safe=retry_safe,
            error_code=error_code,
        )


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """Redacted result of an action attempt.

    ``changed`` means a change was confirmed.  It remains false for an uncertain
    action even though that action may have changed external state.
    """

    status: ActionOutcomeStatus
    dispatched: bool
    changed: bool
    retry_safe: bool
    provider_code: str
    error_code: OperationErrorCode | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionOutcomeStatus):
            raise ValueError("status must be an ActionOutcomeStatus")
        if not all(
            isinstance(value, bool)
            for value in (self.dispatched, self.changed, self.retry_safe)
        ):
            raise ValueError("action outcome flags must be booleans")
        if self.error_code is not None and not isinstance(
            self.error_code, OperationErrorCode
        ):
            raise ValueError("action error_code must be an OperationErrorCode")
        _validate_provider_code(self.provider_code)

        if self.status is ActionOutcomeStatus.SUCCEEDED:
            if not self.dispatched or self.retry_safe or self.error_code is not None:
                raise ValueError("successful action outcome is inconsistent")
            return
        if self.status is ActionOutcomeStatus.OUTCOME_UNKNOWN:
            if (
                self.changed
                or self.retry_safe
                or self.error_code is not OperationErrorCode.OUTCOME_UNKNOWN
            ):
                raise ValueError("unknown action outcome is inconsistent")
            return
        if (
            self.changed
            or self.error_code is None
            or self.error_code is OperationErrorCode.OUTCOME_UNKNOWN
        ):
            raise ValueError("failed action outcome is inconsistent")


class ActionCheckPort(Protocol):
    """Async capability or focus check performed before an action dispatch."""

    def __call__(self, budget: OperationBudget, /) -> Awaitable[bool]: ...


class ActionDispatchPort(Protocol):
    """Async execution of exactly one already-selected provider action.

    Provider-specific completion observation can be contained in this callable.  For
    native actions, integrations can wrap the existing register-before-dispatch event
    primitive here.  The callable must not attempt a second provider strategy.
    """

    def __call__(
        self,
        budget: OperationBudget,
        /,
    ) -> Awaitable[ActionDispatchResult]: ...


@dataclass(frozen=True, slots=True)
class ActionPorts:
    """Injected provider boundary for one action invocation."""

    provider_code: str
    capability: ActionCheckPort
    dispatch: ActionDispatchPort
    focus: ActionCheckPort | None = None

    def __post_init__(self) -> None:
        _validate_provider_code(self.provider_code)
        if not callable(self.capability) or not callable(self.dispatch):
            raise ValueError("action capability and dispatch ports must be callable")
        if self.focus is not None and not callable(self.focus):
            raise ValueError("action focus port must be callable")


def _validate_provider_code(provider_code: str) -> None:
    if not isinstance(provider_code, str) or not _PROVIDER_CODE_PATTERN.fullmatch(
        provider_code
    ):
        raise ValueError("provider_code must be a stable identifier")


def _failed(
    provider_code: str,
    error_code: OperationErrorCode,
    *,
    dispatched: bool,
    retry_safe: bool,
) -> ActionOutcome:
    if error_code is OperationErrorCode.OUTCOME_UNKNOWN:
        return _unknown(provider_code, dispatched=dispatched)
    return ActionOutcome(
        status=ActionOutcomeStatus.FAILED,
        dispatched=dispatched,
        changed=False,
        retry_safe=retry_safe,
        provider_code=provider_code,
        error_code=error_code,
    )


def _unknown(provider_code: str, *, dispatched: bool) -> ActionOutcome:
    return ActionOutcome(
        status=ActionOutcomeStatus.OUTCOME_UNKNOWN,
        dispatched=dispatched,
        changed=False,
        retry_safe=False,
        provider_code=provider_code,
        error_code=OperationErrorCode.OUTCOME_UNKNOWN,
    )


class ActionKernel:
    """Run one selected UI action after bounded capability and focus checks."""

    async def run(
        self,
        operation: TransactionOperation,
        *,
        ports: ActionPorts,
        budget: OperationBudget,
    ) -> ActionOutcome:
        if (
            not isinstance(operation, TransactionOperation)
            or operation not in _ACTION_OPERATIONS
        ):
            raise ValueError("operation must be a supported action")
        if not isinstance(ports, ActionPorts):
            raise ValueError("ports must be ActionPorts")
        if not isinstance(budget, OperationBudget):
            raise ValueError("budget must be an OperationBudget")

        capability_failure = await self._check(
            ports.capability,
            budget=budget,
            provider_code=ports.provider_code,
            phase="action capability preflight",
            failure_code=OperationErrorCode.UNSUPPORTED_CAPABILITY,
        )
        if capability_failure is not None:
            return capability_failure

        if ports.focus is not None:
            focus_failure = await self._check(
                ports.focus,
                budget=budget,
                provider_code=ports.provider_code,
                phase="action focus preflight",
                failure_code=OperationErrorCode.FOCUS_MISMATCH,
            )
            if focus_failure is not None:
                return focus_failure

        try:
            budget.checkpoint("action dispatch")
        except OperationError as exc:
            return _failed(
                ports.provider_code,
                exc.code,
                dispatched=False,
                retry_safe=True,
            )

        # The selected port is invoked in one place and never iterated.  Once this
        # boundary is crossed, cancellation or a deadline cannot prove the provider
        # did not act, so both become an unknown, non-retryable outcome.
        try:
            receipt = await budget.wait_for(
                ports.dispatch(budget),
                operation="action provider dispatch",
            )
        except (OperationError, asyncio.CancelledError):
            return _unknown(ports.provider_code, dispatched=True)
        except Exception:
            return _unknown(ports.provider_code, dispatched=True)

        if not isinstance(receipt, ActionDispatchResult):
            return _unknown(ports.provider_code, dispatched=True)
        if receipt.acknowledged:
            return ActionOutcome(
                status=ActionOutcomeStatus.SUCCEEDED,
                dispatched=True,
                changed=receipt.changed,
                retry_safe=False,
                provider_code=ports.provider_code,
                error_code=None,
            )
        error_code = receipt.error_code
        if error_code is None:
            return _unknown(ports.provider_code, dispatched=True)
        if error_code is OperationErrorCode.OUTCOME_UNKNOWN:
            return _unknown(ports.provider_code, dispatched=True)
        return _failed(
            ports.provider_code,
            error_code,
            dispatched=True,
            retry_safe=receipt.retry_safe,
        )

    @staticmethod
    async def _check(
        check: ActionCheckPort,
        *,
        budget: OperationBudget,
        provider_code: str,
        phase: str,
        failure_code: OperationErrorCode,
    ) -> ActionOutcome | None:
        try:
            budget.checkpoint(phase)
            ready = await budget.wait_for(check(budget), operation=phase)
        except asyncio.CancelledError:
            raise
        except OperationError as exc:
            return _failed(
                provider_code,
                exc.code,
                dispatched=False,
                retry_safe=True,
            )
        except Exception:
            return _failed(
                provider_code,
                failure_code,
                dispatched=False,
                retry_safe=True,
            )
        if ready is not True:
            return _failed(
                provider_code,
                failure_code,
                dispatched=False,
                retry_safe=True,
            )
        return None
