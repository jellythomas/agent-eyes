"""Concurrency-safe lifecycle primitives for automation providers."""
from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, TypeVar

from .observations import ObservationStore
from .operation import OperationBudget, OperationError, OperationErrorCode


_T = TypeVar("_T")


@dataclass(slots=True)
class _KeyedLock:
    lock: asyncio.Lock
    users: int = 0


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[Any]
    waiters: int = 0


class AutomationCoordinator:
    """Own ordering, single-flight observations, and snapshot lifecycle."""

    def __init__(
        self,
        observations: ObservationStore | None = None,
        *,
        shutdown_timeout: float = 0.1,
    ) -> None:
        if (
            isinstance(shutdown_timeout, bool)
            or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(float(shutdown_timeout))
            or shutdown_timeout < 0
        ):
            raise ValueError("shutdown_timeout must be finite and non-negative")
        self.observations = observations or ObservationStore()
        self._shutdown_timeout = float(shutdown_timeout)
        self._foreground_lock = asyncio.Lock()
        self._shadow_locks: dict[Hashable, _KeyedLock] = {}
        self._flights: dict[Hashable, _Flight] = {}
        self._flights_guard = asyncio.Lock()
        self._foreground_poison: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def observe(
        self,
        key: Hashable,
        producer: Callable[[], Awaitable[_T]],
        *,
        budget: OperationBudget | None = None,
    ) -> _T:
        self._ensure_open()
        async with self._flights_guard:
            self._ensure_open()
            flight = self._flights.get(key)
            if flight is None:
                task = asyncio.create_task(producer())
                flight = _Flight(task=task)
                self._flights[key] = flight
                task.add_done_callback(
                    lambda done, flight_key=key: self._flight_finished(
                        flight_key, done
                    )
                )
            flight.waiters += 1
            task = flight.task
        try:
            shielded = asyncio.shield(task)
            if budget is not None:
                return await budget.wait_for(
                    shielded,
                    operation="observation provider",
                )
            return await shielded
        finally:
            self._release_flight_waiter(key, flight)

    async def execute_foreground(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        budget: OperationBudget | None = None,
        operation_manages_deadline: bool = False,
    ) -> _T:
        self._ensure_open()
        self._raise_if_foreground_poisoned()
        return await self._execute_locked(
            self._foreground_lock,
            operation,
            budget=budget,
            operation_manages_deadline=operation_manages_deadline,
            queue_name="foreground mutation queue",
            operation_name="foreground mutation",
            foreground=True,
        )

    def poison_foreground_until(self, recovery: Awaitable[Any]) -> None:
        """Reject later foreground work until uncertain provider work settles."""
        self._ensure_open()
        task = asyncio.create_task(
            recovery,
            name="agent-eyes-foreground-recovery",
        )
        self._foreground_poison.add(task)
        task.add_done_callback(self._foreground_recovered)

    async def execute_shadow(
        self,
        target_id: Hashable,
        operation: Callable[[], Awaitable[_T]],
        *,
        budget: OperationBudget | None = None,
        operation_manages_deadline: bool = False,
    ) -> _T:
        self._ensure_open()
        entry = self._retain_shadow_lock(target_id)
        try:
            return await self._execute_locked(
                entry.lock,
                operation,
                budget=budget,
                operation_manages_deadline=operation_manages_deadline,
                queue_name="shadow target mutation queue",
                operation_name="shadow target mutation",
                foreground=False,
            )
        finally:
            self._release_shadow_lock(target_id, entry)

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close(), name="agent-eyes-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        async with self._flights_guard:
            tasks = tuple(flight.task for flight in self._flights.values())
        tasks += tuple(self._foreground_poison)
        if tasks:
            for task in tasks:
                task.cancel()
            done, _pending = await asyncio.wait(
                tasks,
                timeout=self._shutdown_timeout,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        async with self._flights_guard:
            self._flights.clear()
        self.observations.close()

    async def _execute_locked(
        self,
        lock: asyncio.Lock,
        operation: Callable[[], Awaitable[_T]],
        *,
        budget: OperationBudget | None,
        operation_manages_deadline: bool,
        queue_name: str,
        operation_name: str,
        foreground: bool,
    ) -> _T:
        acquired = False
        try:
            if budget is None:
                await lock.acquire()
            else:
                await budget.wait_for(lock.acquire(), operation=queue_name)
            acquired = True
            if foreground:
                self._raise_if_foreground_poisoned()
            if budget is None or operation_manages_deadline:
                return await operation()
            budget.checkpoint(operation_name)
            return await budget.wait_for(operation(), operation=operation_name)
        finally:
            if acquired:
                lock.release()

    def _retain_shadow_lock(self, target_id: Hashable) -> _KeyedLock:
        entry = self._shadow_locks.get(target_id)
        if entry is None:
            entry = _KeyedLock(asyncio.Lock())
            self._shadow_locks[target_id] = entry
        entry.users += 1
        return entry

    def _release_shadow_lock(
        self,
        target_id: Hashable,
        entry: _KeyedLock,
    ) -> None:
        entry.users -= 1
        if entry.users == 0 and self._shadow_locks.get(target_id) is entry:
            self._shadow_locks.pop(target_id, None)

    def _flight_finished(
        self,
        key: Hashable,
        task: asyncio.Task[Any],
    ) -> None:
        if not task.cancelled():
            task.exception()
        flight = self._flights.get(key)
        if flight is not None and flight.task is task:
            self._flights.pop(key, None)

    def _release_flight_waiter(self, key: Hashable, flight: _Flight) -> None:
        flight.waiters -= 1
        if flight.waiters == 0:
            if not flight.task.done():
                flight.task.cancel()
            if self._flights.get(key) is flight:
                self._flights.pop(key, None)

    def _raise_if_foreground_poisoned(self) -> None:
        if any(not task.done() for task in self._foreground_poison):
            raise OperationError(
                OperationErrorCode.PROVIDER_BUSY,
                "a prior foreground mutation may still be running",
            )

    def _foreground_recovered(self, task: asyncio.Task[Any]) -> None:
        self._foreground_poison.discard(task)
        if not task.cancelled():
            task.exception()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("automation coordinator is closed")
