"""Pure bounded state machine for one provider-neutral UI transaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Awaitable, Callable, Protocol

from .action_kernel import ActionKernel, ActionOutcomeStatus, ActionPorts
from .adapters.base import UIElement
from .locators import LocatorIndex
from .operation import OperationBudget, OperationError, OperationErrorCode
from .transaction_contract import (
    ExecuteRequest,
    Locator,
    TargetMode,
    TargetSpec,
    TransactionOperation,
    TransactionStep,
)


_ACTION_OPERATIONS = frozenset(
    {
        TransactionOperation.HOVER,
        TransactionOperation.CLICK,
        TransactionOperation.TYPE,
        TransactionOperation.PRESS_KEY,
        TransactionOperation.SCROLL,
    }
)


class TransactionStatus(str, Enum):
    """Stable terminal state of one bounded transaction."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class TransactionTarget:
    """Exact resolved target plus an optional integration-owned opaque value."""

    target_id: str
    replay_unsafe: bool = False
    value: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not self.target_id:
            raise ValueError("transaction target requires an exact target identifier")
        if not isinstance(self.replay_unsafe, bool):
            raise ValueError("transaction target replay_unsafe must be a boolean")


@dataclass(frozen=True, slots=True)
class TransactionView:
    """One immutable locator revision used only inside a transaction."""

    index: LocatorIndex
    snapshot: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.index, LocatorIndex):
            raise ValueError("transaction view requires a LocatorIndex")
        if not isinstance(self.snapshot, str):
            raise ValueError("transaction snapshot must be a string")


class ResolveTransactionTargetPort(Protocol):
    """Resolve one exact target and optionally activate it in the same call."""

    def __call__(
        self,
        spec: TargetSpec,
        activate: bool,
        budget: OperationBudget,
        /,
    ) -> Awaitable[TransactionTarget]: ...


class ObserveTransactionTargetPort(Protocol):
    """Capture the transaction's only initial full observation."""

    def __call__(
        self,
        target: TransactionTarget,
        budget: OperationBudget,
        /,
    ) -> Awaitable[TransactionView]: ...


class RefreshTransactionTargetPort(Protocol):
    """Refresh the affected target scope for a pending locator."""

    def __call__(
        self,
        target: TransactionTarget,
        current: TransactionView,
        locator: Locator,
        budget: OperationBudget,
        /,
    ) -> Awaitable[TransactionView]: ...


class BuildActionPorts(Protocol):
    """Bind one step and resolved element to one action-kernel provider."""

    def __call__(
        self,
        step: TransactionStep,
        element: UIElement | None,
        target: TransactionTarget,
        /,
    ) -> ActionPorts: ...


@dataclass(frozen=True, slots=True)
class TransactionPorts:
    """Injected provider boundaries used by the pure state machine."""

    resolve: ResolveTransactionTargetPort
    observe: ObserveTransactionTargetPort
    refresh: RefreshTransactionTargetPort
    action_ports: BuildActionPorts

    def __post_init__(self) -> None:
        if not all(
            callable(port)
            for port in (self.resolve, self.observe, self.refresh, self.action_ports)
        ):
            raise ValueError("transaction ports must be callable")


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Compact user-safe state; it never contains steps, locators, or typed text."""

    status: TransactionStatus
    code: OperationErrorCode | None
    target_id: str
    completed_steps: int
    failed_step: int | None
    elapsed_ms: int
    retry_safe: bool
    final_expectation: bool | None
    snapshot: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, TransactionStatus):
            raise ValueError("status must be a TransactionStatus")
        if not isinstance(self.target_id, str) or not isinstance(self.snapshot, str):
            raise ValueError("transaction result identifiers must be strings")
        if (
            isinstance(self.completed_steps, bool)
            or not isinstance(self.completed_steps, int)
            or self.completed_steps < 0
        ):
            raise ValueError("completed_steps must be a non-negative integer")
        if self.failed_step is not None and (
            isinstance(self.failed_step, bool)
            or not isinstance(self.failed_step, int)
            or self.failed_step < 1
        ):
            raise ValueError("failed_step must be a positive one-based index")
        if (
            isinstance(self.elapsed_ms, bool)
            or not isinstance(self.elapsed_ms, int)
            or self.elapsed_ms < 0
        ):
            raise ValueError("elapsed_ms must be a non-negative integer")
        if not isinstance(self.retry_safe, bool):
            raise ValueError("retry_safe must be a boolean")
        if self.final_expectation not in (None, True, False):
            raise ValueError("final_expectation must be a boolean or None")
        if self.status is TransactionStatus.SUCCEEDED:
            if self.code is not None or self.failed_step is not None:
                raise ValueError("successful transaction result is inconsistent")
        elif self.code is None:
            raise ValueError("failed transaction result requires an error code")
        if self.status is TransactionStatus.OUTCOME_UNKNOWN and (
            self.code is not OperationErrorCode.OUTCOME_UNKNOWN or self.retry_safe
        ):
            raise ValueError("unknown transaction result is inconsistent")


class TransactionEngine:
    """Execute validated steps linearly without model or server round trips."""

    def __init__(
        self,
        *,
        action_kernel: ActionKernel | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._action_kernel = action_kernel or ActionKernel()
        self._clock = clock

    async def run(
        self,
        request: ExecuteRequest,
        *,
        ports: TransactionPorts,
        budget: OperationBudget | None = None,
    ) -> TransactionResult:
        if not isinstance(request, ExecuteRequest):
            raise ValueError("request must be a validated ExecuteRequest")
        if not isinstance(ports, TransactionPorts):
            raise ValueError("ports must be TransactionPorts")
        if budget is not None and not isinstance(budget, OperationBudget):
            raise ValueError("budget must be an OperationBudget")

        started_at = self._clock()
        transaction_budget = (
            OperationBudget.start(request.deadline_ms / 1_000, clock=self._clock)
            if budget is None
            else budget.child(request.deadline_ms / 1_000)
        )
        target: TransactionTarget | None = None
        view: TransactionView | None = None
        aliases: dict[str, UIElement] = {}
        alias_locators: dict[str, Locator] = {}
        dirty = False
        replay_unsafe = False
        completed_steps = 0

        def result(
            status: TransactionStatus,
            code: OperationErrorCode | None,
            *,
            failed_step: int | None,
            retry_safe: bool,
            final_expectation: bool | None = None,
        ) -> TransactionResult:
            return TransactionResult(
                status=status,
                code=code,
                target_id=target.target_id if target is not None else "",
                completed_steps=completed_steps,
                failed_step=failed_step,
                elapsed_ms=max(0, int((self._clock() - started_at) * 1_000)),
                retry_safe=retry_safe,
                final_expectation=final_expectation,
                snapshot=(
                    view.snapshot
                    if view is not None
                    and not dirty
                    and status is not TransactionStatus.OUTCOME_UNKNOWN
                    else ""
                ),
            )

        def failure(
            code: OperationErrorCode,
            *,
            failed_step: int | None,
            retry_hint: bool = True,
            final_expectation: bool | None = None,
        ) -> TransactionResult:
            if code is OperationErrorCode.OUTCOME_UNKNOWN:
                return result(
                    TransactionStatus.OUTCOME_UNKNOWN,
                    code,
                    failed_step=failed_step,
                    retry_safe=False,
                    final_expectation=final_expectation,
                )
            return result(
                TransactionStatus.FAILED,
                code,
                failed_step=failed_step,
                retry_safe=(
                    retry_hint
                    and not replay_unsafe
                    and request.consequential_step is None
                ),
                final_expectation=final_expectation,
            )

        try:
            transaction_budget.checkpoint("transaction target resolution")
            target = await transaction_budget.wait_for(
                ports.resolve(
                    request.target,
                    request.target.mode is TargetMode.FOREGROUND,
                    transaction_budget,
                ),
                operation="transaction target resolution",
            )
            if not isinstance(target, TransactionTarget):
                raise TypeError("invalid resolved transaction target")
            replay_unsafe = target.replay_unsafe
        except asyncio.CancelledError:
            raise
        except OperationError as exc:
            return failure(exc.code, failed_step=None)
        except Exception:
            return failure(OperationErrorCode.PROVIDER_BUSY, failed_step=None)

        async def view_for(locator: Locator | None) -> TransactionView:
            nonlocal dirty, view
            transaction_budget.checkpoint("transaction observation")
            if view is None:
                loaded = await transaction_budget.wait_for(
                    ports.observe(target, transaction_budget),
                    operation="transaction initial observation",
                )
            elif dirty:
                if locator is None:
                    raise OperationError(
                        OperationErrorCode.INVALID_TRANSACTION,
                        "a scoped refresh requires a locator",
                    )
                loaded = await transaction_budget.wait_for(
                    ports.refresh(target, view, locator, transaction_budget),
                    operation="transaction scoped refresh",
                )
            else:
                return view
            if not isinstance(loaded, TransactionView):
                raise TypeError("invalid transaction view")
            view = loaded
            dirty = False
            return view

        def resolve_locator(current: TransactionView, locator: Locator) -> UIElement:
            scoped_aliases = dict(aliases)
            resolving: set[str] = set()

            def resolve_scope(alias: str) -> None:
                if alias in scoped_aliases:
                    return
                definition = alias_locators.get(alias)
                if definition is None or alias in resolving:
                    raise OperationError(
                        OperationErrorCode.ELEMENT_NOT_FOUND,
                        "locator scope is unavailable in the current revision",
                    )
                resolving.add(alias)
                try:
                    if definition.within:
                        resolve_scope(definition.within)
                    scoped_aliases[alias] = current.index.resolve_unique(
                        definition,
                        aliases=scoped_aliases,
                    )
                finally:
                    resolving.remove(alias)

            if locator.within:
                resolve_scope(locator.within)
            return current.index.resolve_unique(locator, aliases=scoped_aliases)

        for step in request.steps:
            step_number = step.index + 1
            try:
                transaction_budget.checkpoint("transaction step")
                if step.operation is TransactionOperation.LOCATE:
                    if step.locator is None or not step.alias:
                        raise OperationError(
                            OperationErrorCode.INVALID_TRANSACTION,
                            "locate step is incomplete",
                        )
                    current = await view_for(step.locator)
                    aliases[step.alias] = resolve_locator(current, step.locator)
                    alias_locators[step.alias] = step.locator
                    completed_steps += 1
                    continue

                if step.operation is TransactionOperation.EXPECT:
                    if step.locator is None:
                        raise OperationError(
                            OperationErrorCode.INVALID_TRANSACTION,
                            "expect step is incomplete",
                        )
                    current = await view_for(step.locator)
                    resolve_locator(current, step.locator)
                    completed_steps += 1
                    continue

                if step.operation not in _ACTION_OPERATIONS:
                    raise OperationError(
                        OperationErrorCode.INVALID_TRANSACTION,
                        "transaction operation is unsupported",
                    )

                if view is None:
                    await view_for(None)

                element: UIElement | None = None
                if step.ref:
                    element = aliases.get(step.ref)
                    if element is None:
                        raise OperationError(
                            OperationErrorCode.ELEMENT_NOT_FOUND,
                            "transaction-local action reference is unavailable",
                        )
                elif step.operation is not TransactionOperation.SCROLL:
                    raise OperationError(
                        OperationErrorCode.INVALID_TRANSACTION,
                        "action step requires a transaction-local reference",
                    )

                selected_ports = ports.action_ports(step, element, target)
                if not isinstance(selected_ports, ActionPorts):
                    raise TypeError("invalid action ports")
                outcome = await self._action_kernel.run(
                    step.operation,
                    ports=selected_ports,
                    budget=transaction_budget,
                )
                if outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN:
                    dirty = True
                    aliases.clear()
                    return failure(
                        OperationErrorCode.OUTCOME_UNKNOWN,
                        failed_step=step_number,
                    )
                if outcome.status is ActionOutcomeStatus.FAILED:
                    if outcome.dispatched:
                        dirty = True
                        aliases.clear()
                    return failure(
                        outcome.error_code or OperationErrorCode.PROVIDER_BUSY,
                        failed_step=step_number,
                        retry_hint=outcome.retry_safe,
                    )

                replay_unsafe = True
                dirty = True
                aliases.clear()
                if step.expect is not None:
                    current = await view_for(step.expect)
                    resolve_locator(current, step.expect)
                completed_steps += 1
            except asyncio.CancelledError:
                if replay_unsafe:
                    return failure(
                        OperationErrorCode.OUTCOME_UNKNOWN,
                        failed_step=step_number,
                    )
                raise
            except OperationError as exc:
                return failure(exc.code, failed_step=step_number)
            except Exception:
                return failure(
                    OperationErrorCode.UNSUPPORTED_CAPABILITY,
                    failed_step=step_number,
                )

        if request.final_expect is not None:
            try:
                transaction_budget.checkpoint("transaction final expectation")
                current = await view_for(request.final_expect)
                resolve_locator(current, request.final_expect)
            except asyncio.CancelledError:
                if replay_unsafe:
                    return failure(
                        OperationErrorCode.OUTCOME_UNKNOWN,
                        failed_step=None,
                        final_expectation=False,
                    )
                raise
            except OperationError as exc:
                return failure(
                    exc.code,
                    failed_step=None,
                    final_expectation=False,
                )
            except Exception:
                return failure(
                    OperationErrorCode.PROVIDER_BUSY,
                    failed_step=None,
                    final_expectation=False,
                )

        return result(
            TransactionStatus.SUCCEEDED,
            None,
            failed_step=None,
            retry_safe=False,
            final_expectation=(True if request.final_expect is not None else None),
        )
