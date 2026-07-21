"""Serialized provider-owned worker with deadline-safe quarantine semantics."""
from __future__ import annotations

import asyncio
import math
import queue
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from typing import TypeVar

from .operation import OperationBudget, OperationError


_T = TypeVar("_T")
_STOP = object()
_HANDOFF_LIVENESS_TIMEOUT_SECONDS = 0.05


class _DaemonSerialExecutor:
    """Minimal lazy single-thread executor that cannot pin process shutdown."""

    def __init__(self, thread_name: str) -> None:
        self._thread_name = thread_name
        self._queue: queue.Queue[object] = queue.Queue()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._shutdown = False

    def submit(self, call: Callable[[], _T]) -> ConcurrentFuture[_T]:
        future: ConcurrentFuture[_T] = ConcurrentFuture()
        with self._guard:
            if self._shutdown:
                raise RuntimeError("provider executor is shut down")
            self._queue.put((future, call))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name=self._thread_name,
                    daemon=True,
                )
                self._thread.start()
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        with self._guard:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    self._cancel_queued()
                self._queue.put(_STOP)
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join()

    def _cancel_queued(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                continue
            future, _call = item
            future.cancel()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            future, call = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = call()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)


class ProviderCallState:
    """Thread-safe lifecycle evidence for one provider call."""

    def __init__(self) -> None:
        self._submitted = threading.Event()
        self._started = threading.Event()
        self._uncertain = threading.Event()

    @property
    def submitted(self) -> bool:
        return self._submitted.is_set()

    @property
    def started(self) -> bool:
        return self._started.is_set()

    @property
    def may_have_run(self) -> bool:
        return self.started or self._uncertain.is_set()

    def _mark_submitted(self) -> None:
        self._submitted.set()

    def _mark_started(self) -> None:
        self._started.set()

    def _mark_uncertain(self) -> None:
        self._uncertain.set()


@dataclass(slots=True)
class _LaneWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    active: bool = True
    granted: bool = False


class ProviderWorker:
    """Run synchronous provider calls off-loop without overlapping late work."""

    def __init__(self, name: str, *, shutdown_timeout: float = 0.1) -> None:
        if not name:
            raise ValueError("provider worker name is required")
        if (
            isinstance(shutdown_timeout, bool)
            or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(float(shutdown_timeout))
            or shutdown_timeout < 0
        ):
            raise ValueError("shutdown_timeout must be finite and non-negative")
        self._name = name
        self._shutdown_timeout = float(shutdown_timeout)
        self._executor = _DaemonSerialExecutor(f"agent-eyes-{name}")
        self._guard = threading.Lock()
        # OrderedDict keeps FIFO handoff while allowing O(1) cancellation unlinking.
        self._waiters: OrderedDict[asyncio.Future[None], _LaneWaiter] = OrderedDict()
        self._handoff: _LaneWaiter | None = None
        self._occupied = False
        self._active: ConcurrentFuture[object] | None = None
        self._closed = False
        self._shutdown_complete = False

    @property
    def busy(self) -> bool:
        with self._guard:
            return self._occupied

    async def run(
        self,
        call: Callable[[], _T],
        *,
        budget: OperationBudget,
        operation: str,
        state: ProviderCallState | None = None,
    ) -> _T:
        """Run one sync call; timed-out active work retains its lane until finished."""
        if not operation:
            raise ValueError("operation name is required")
        self._ensure_open()
        call_state = state or ProviderCallState()
        await self._acquire_lane(budget=budget)
        future: ConcurrentFuture[_T] | None = None
        try:
            self._ensure_open()
            budget.checkpoint(f"{self._name} provider dispatch")

            def tracked_call() -> _T:
                # A request may expire while queued for the OS-owned thread.
                # Check before marking it dispatched or touching the provider.
                budget.checkpoint(f"{self._name} provider dispatch")
                call_state._mark_started()
                return call()

            future = self._executor.submit(tracked_call)
            call_state._mark_submitted()
            with self._guard:
                self._active = future
            future.add_done_callback(self._complete_call)
        except BaseException:
            if future is None:
                self._release_lane()
            raise

        wrapped = asyncio.wrap_future(future)
        try:
            return await budget.wait_for(wrapped, operation=operation)
        except (OperationError, asyncio.CancelledError):
            # asyncio cancellation attempts to cancel the concurrent Future.
            # If cancellation loses the race to a running worker, conservatively
            # classify a mutation as possibly dispatched until the lane clears.
            if not future.cancel() and not future.done():
                call_state._mark_uncertain()
            raise

    async def wait_until_idle(self) -> None:
        """Wait for the lane and hand it on without relying on an event loop lock."""
        await self._acquire_lane(budget=None, allow_closed=True)
        self._release_lane()

    async def aclose(self) -> None:
        """Close once without letting a hung OS provider pin process exit."""
        with self._guard:
            self._closed = True
            if self._shutdown_complete:
                return

        try:
            await self._acquire_lane(
                budget=OperationBudget.start(self._shutdown_timeout),
                allow_closed=True,
            )
        except OperationError:
            self._executor.shutdown(wait=False, cancel_futures=True)
            with self._guard:
                self._shutdown_complete = True
            return
        try:
            with self._guard:
                if self._shutdown_complete:
                    return
            # No provider call can be active while this close owns the lane.
            self._executor.shutdown(wait=True, cancel_futures=True)
            with self._guard:
                self._shutdown_complete = True
        finally:
            self._release_lane()

    async def _acquire_lane(
        self,
        *,
        budget: OperationBudget | None,
        allow_closed: bool = False,
    ) -> None:
        loop = asyncio.get_running_loop()
        waiter: _LaneWaiter | None = None
        recover_stale_handoff = False
        with self._guard:
            if self._closed and not allow_closed:
                raise RuntimeError(f"{self._name} provider worker is closed")
            handoff = self._handoff
            if handoff is not None and (
                not handoff.active
                or handoff.future.cancelled()
                or handoff.loop.is_closed()
            ):
                handoff.active = False
                handoff.granted = False
                self._handoff = None
                if not self._waiters:
                    return
                recover_stale_handoff = True
            if not self._occupied:
                self._occupied = True
                return
            waiter = _LaneWaiter(loop=loop, future=loop.create_future())
            self._waiters[waiter.future] = waiter

        if recover_stale_handoff:
            self._release_lane()

        try:
            if budget is None:
                while not waiter.future.done():
                    done, _pending = await asyncio.wait(
                        (waiter.future,),
                        timeout=_HANDOFF_LIVENESS_TIMEOUT_SECONDS,
                    )
                    if done:
                        break
                    self._reclaim_abandoned_handoff()
                await waiter.future
            else:
                await budget.wait_for(
                    waiter.future,
                    operation=f"{self._name} provider queue",
                )
        except BaseException:
            should_handoff = False
            with self._guard:
                waiter.active = False
                self._waiters.pop(waiter.future, None)
                if waiter.granted:
                    waiter.granted = False
                    if self._handoff is waiter:
                        self._handoff = None
                    should_handoff = True
            if should_handoff:
                self._release_lane()
            else:
                self._reclaim_abandoned_handoff()
            raise
        with self._guard:
            waiter.active = False
            waiter.granted = False
            if self._handoff is waiter:
                self._handoff = None

    def _complete_call(self, future: ConcurrentFuture[object]) -> None:
        """Consume late exceptions privately and release the cross-loop lane."""
        try:
            future.exception()
        except BaseException:
            pass
        with self._guard:
            if self._active is future:
                self._active = None
        self._release_lane()

    def _release_lane(self) -> None:
        with self._guard:
            while self._waiters:
                _future, waiter = self._waiters.popitem(last=False)
                if (
                    not waiter.active
                    or waiter.future.done()
                    or waiter.loop.is_closed()
                ):
                    waiter.active = False
                    continue
                waiter.granted = True
                self._handoff = waiter
                try:
                    waiter.loop.call_soon_threadsafe(
                        self._resolve_waiter,
                        waiter,
                    )
                except RuntimeError:
                    waiter.active = False
                    waiter.granted = False
                    if self._handoff is waiter:
                        self._handoff = None
                    continue
                return
            self._occupied = False

    def _reclaim_abandoned_handoff(self) -> bool:
        """Recover ownership reserved for a waiter whose event loop disappeared."""
        should_handoff = False
        with self._guard:
            handoff = self._handoff
            if handoff is None or (
                handoff.active
                and not handoff.future.cancelled()
                and not handoff.loop.is_closed()
            ):
                return False
            handoff.active = False
            handoff.granted = False
            self._handoff = None
            if self._waiters:
                should_handoff = True
            else:
                self._occupied = False
        if should_handoff:
            self._release_lane()
        return True

    def _resolve_waiter(self, waiter: _LaneWaiter) -> None:
        should_handoff = False
        with self._guard:
            if self._handoff is not waiter:
                return
            if waiter.future.done() or not waiter.active:
                waiter.active = False
                waiter.granted = False
                self._handoff = None
                should_handoff = True
        if should_handoff:
            self._release_lane()
            return
        waiter.future.set_result(None)

    def _ensure_open(self) -> None:
        with self._guard:
            if self._closed:
                raise RuntimeError(f"{self._name} provider worker is closed")
