"""Event-driven foreground accessibility waits.

The public wait helper subscribes to the current platform's accessibility
notifications before its first tree query.  A generation counter prevents a
notification fired during a query from being lost.  Tree polling is used only
when the native notification API cannot be registered.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .adapters.base import UIElement
from .operation import OperationBudget, OperationError, OperationErrorCode
from .provider_worker import ProviderCallState, ProviderWorker


logger = logging.getLogger("agent-eyes.native-events")

_EVENT_STARTUP_TIMEOUT = 0.75
_FALLBACK_INITIAL_INTERVAL = 0.08
_FALLBACK_MAX_INTERVAL = 0.5
_FALLBACK_GROWTH = 1.7
_MAX_FALLBACK_CHECKS = 128
_EVENT_WATCHDOG_INTERVAL = 0.5
_EVENT_WATCHDOG_DEADLINE_RESERVE = 0.1


class _ChangeSubscription(Protocol):
    @property
    def active(self) -> bool: ...

    @property
    def generation(self) -> int: ...

    async def wait_for_change(self, after_generation: int, timeout: float) -> bool: ...

    async def aclose(self) -> None: ...


SubscriptionFactory = Callable[[int], Awaitable[_ChangeSubscription | None]]


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    """Retrieve a detached task result so late cleanup cannot log warnings."""
    try:
        task.result()
    except BaseException:
        pass


async def _await_with_budget(
    awaitable: Awaitable[Any],
    *,
    budget: OperationBudget,
    operation: str,
) -> Any:
    """Bound cancellation-aware async provider work by the request deadline."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await budget.wait_for(asyncio.shield(task), operation=operation)
    except BaseException:
        if not task.done():
            task.cancel()
        task.add_done_callback(_consume_task_result)
        raise


async def _run_sync_with_budget(
    call: Callable[[], Any],
    *,
    budget: OperationBudget,
    operation: str,
    worker: ProviderWorker | None,
    state: ProviderCallState | None = None,
    pre_dispatch: Callable[[], None] | None = None,
) -> Any:
    """Run sync provider work off-loop and return promptly on deadline expiry."""
    if worker is not None:
        return await worker.run(
            call,
            budget=budget,
            operation=operation,
            state=state,
            pre_dispatch=pre_dispatch,
        )

    def guarded_call() -> Any:
        if pre_dispatch is not None:
            pre_dispatch()
        return call()

    task = asyncio.create_task(asyncio.to_thread(guarded_call))
    try:
        return await budget.wait_for(asyncio.shield(task), operation=operation)
    except BaseException:
        # A running thread cannot be cancelled safely.  Retrieve its eventual
        # result while the caller returns at the deadline.  Production callers
        # pass a ProviderWorker, which also quarantines its serialized lane.
        task.add_done_callback(_consume_task_result)
        raise


async def _close_with_budget(
    subscription: _ChangeSubscription,
    *,
    budget: OperationBudget,
) -> None:
    """Start cleanup without allowing it to extend the caller's deadline."""
    task = asyncio.create_task(subscription.aclose())
    task.add_done_callback(_consume_task_result)
    if budget.expired:
        # Give immediate close implementations one scheduler turn.  Slow
        # provider cleanup remains detached and cannot extend the deadline.
        await asyncio.sleep(0)
        return
    try:
        await budget.wait_for(
            asyncio.shield(task),
            operation="native observer cleanup",
        )
    except OperationError as exc:
        if exc.code is not OperationErrorCode.DEADLINE_EXCEEDED:
            raise


async def _wait_for_change_or_watchdog(
    subscription: _ChangeSubscription,
    after_generation: int,
    *,
    budget: OperationBudget,
    operation: str,
    watchdog_rechecks: int,
) -> tuple[bool, bool]:
    """Wait for a native event while retaining one low-rate safety recheck.

    Returns ``(changed, watchdog_elapsed)``.  The first watchdog reserves part
    of a short request deadline for the verification query.  Later watchdogs
    retain the full interval so an early platform timer cannot cause a burst of
    rechecks.  Generation checks in the subscription make cancelling and
    restarting the event wait lossless at the boundary.
    """
    remaining = budget.remaining()
    budget.checkpoint(operation)
    if watchdog_rechecks == 0:
        watchdog_delay = min(
            _EVENT_WATCHDOG_INTERVAL,
            max(0.0, remaining - _EVENT_WATCHDOG_DEADLINE_RESERVE),
        )
    else:
        watchdog_delay = (
            _EVENT_WATCHDOG_INTERVAL
            if remaining > _EVENT_WATCHDOG_INTERVAL
            else 0.0
        )
    if watchdog_delay <= 0:
        changed = await _await_with_budget(
            subscription.wait_for_change(after_generation, remaining),
            budget=budget,
            operation=operation,
        )
        return bool(changed), False

    change_task = asyncio.create_task(
        subscription.wait_for_change(after_generation, remaining)
    )
    watchdog_task = asyncio.create_task(asyncio.sleep(watchdog_delay))
    tasks = (change_task, watchdog_task)
    try:
        done, _pending = await budget.wait_for(
            asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED),
            operation=operation,
        )
        if change_task in done:
            return bool(change_task.result()), False
        return False, True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
            task.add_done_callback(_consume_task_result)


@dataclass(frozen=True, slots=True)
class NativeWaitResult:
    """Outcome of one native accessibility wait."""

    element: UIElement | None
    elapsed: float
    event_driven: bool
    checks: int


@dataclass(frozen=True, slots=True)
class NativeActionResult:
    """Outcome of an action whose completion is observed through OS events."""

    action_result: Any
    condition_met: bool
    elapsed: float
    event_driven: bool
    checks: int
    action_dispatched: bool = True


class _CompositeChangeSubscription:
    """Merge several process observers into one lossless action subscription."""

    def __init__(self, subscriptions: tuple[_ChangeSubscription, ...]) -> None:
        self._subscriptions = subscriptions
        self._baselines = tuple(subscription.generation for subscription in subscriptions)

    @property
    def active(self) -> bool:
        return all(getattr(subscription, "active", True) for subscription in self._subscriptions)

    @property
    def generation(self) -> int:
        self._baselines = tuple(
            subscription.generation for subscription in self._subscriptions
        )
        return sum(self._baselines)

    async def wait_for_change(self, _after_generation: int, timeout: float) -> bool:
        baselines = self._baselines
        if any(
            subscription.generation > baseline
            for subscription, baseline in zip(self._subscriptions, baselines)
        ):
            return True
        tasks = {
            asyncio.create_task(subscription.wait_for_change(baseline, timeout))
            for subscription, baseline in zip(self._subscriptions, baselines)
        }
        try:
            while tasks:
                done, tasks = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if any(bool(task.result()) for task in done):
                    return True
            return False
        finally:
            for task in tasks:
                task.cancel()
                task.add_done_callback(_consume_task_result)

    async def aclose(self) -> None:
        await asyncio.gather(
            *(subscription.aclose() for subscription in self._subscriptions),
            return_exceptions=True,
        )


class NativeChangeSubscription:
    """Thread-backed, lossless bridge from an OS callback to asyncio."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._ready: asyncio.Event | None = None
        self._available = False
        self._ended = False
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def generation(self) -> int:
        with self._generation_lock:
            return self._generation

    @property
    def active(self) -> bool:
        with self._generation_lock:
            ended = self._ended
        return self._available and not ended and not self._closed

    async def start(self, timeout: float = _EVENT_STARTUP_TIMEOUT) -> bool:
        """Start the platform observer and wait for registration to finish."""
        if self._thread is not None:
            return self._available

        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._thread = threading.Thread(
            target=self._thread_entry,
            name=f"agent-eyes-native-events-{self.pid}",
            daemon=True,
        )
        self._thread.start()

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            logger.debug("Native event registration timed out for PID %s", self.pid)
            await self.aclose()
            return False
        return self.active

    async def wait_for_change(self, after_generation: int, timeout: float) -> bool:
        """Wait until a generation newer than ``after_generation`` exists.

        The event is cleared before the generation is checked.  A callback that
        races with the clear either increments the generation before that check
        or sets the event afterward, so no notification can be lost.
        """
        if timeout <= 0:
            return self.generation > after_generation

        if self._wake is None:
            return self.generation > after_generation

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._wake.clear()
            with self._generation_lock:
                if self._generation > after_generation:
                    return True
                if self._ended:
                    return False

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # Event-loop timers may wake up to one clock tick early.  Keep
                # the subscription authoritative until its absolute deadline;
                # otherwise callers can enter polling fallback prematurely.
                if deadline - loop.time() <= 0:
                    return False

    async def aclose(self) -> None:
        """Stop the observer and join its owner thread."""
        if self._closed:
            return
        self._closed = True
        self._stop_requested.set()
        self._request_platform_stop()

        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 1.0)
            if thread.is_alive():
                logger.debug(
                    "Native event thread did not stop promptly for PID %s", self.pid
                )

    def _signal_change(self) -> None:
        with self._generation_lock:
            self._generation += 1
        loop = self._loop
        wake = self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def _publish_ready(self, available: bool) -> None:
        self._available = available
        ready = self._ready
        if ready is not None:
            ready.set()

    def _mark_ready(self, available: bool) -> None:
        loop = self._loop
        if loop is None:
            self._publish_ready(available)
            return
        try:
            loop.call_soon_threadsafe(self._publish_ready, available)
        except RuntimeError:
            self._available = available

    def _mark_ended(self) -> None:
        with self._generation_lock:
            self._ended = True
            self._generation += 1
        loop = self._loop
        wake = self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def _thread_entry(self) -> None:
        try:
            self._run_backend()
        except Exception as exc:
            logger.debug(
                "Native event registration failed for PID %s "
                "(exception_type=%s)",
                self.pid,
                type(exc).__name__,
            )
            self._mark_ready(False)
        finally:
            self._mark_ended()

    def _run_backend(self) -> None:
        self._mark_ready(False)

    def _request_platform_stop(self) -> None:
        pass


class MacOSAXChangeSubscription(NativeChangeSubscription):
    """macOS AXObserver subscription hosted on its own CFRunLoop."""

    _NOTIFICATION_NAMES = (
        "kAXCreatedNotification",
        "kAXWindowCreatedNotification",
        "kAXSheetCreatedNotification",
        "kAXMenuOpenedNotification",
        "kAXUIElementDestroyedNotification",
        "kAXFocusedUIElementChangedNotification",
        "kAXFocusedWindowChangedNotification",
        "kAXMainWindowChangedNotification",
        "kAXValueChangedNotification",
        "kAXTitleChangedNotification",
        "kAXLayoutChangedNotification",
        "kAXSelectedChildrenChangedNotification",
        "kAXSelectedRowsChangedNotification",
        "kAXSelectedTextChangedNotification",
    )
    _MUTATION_CRITICAL_NOTIFICATIONS = frozenset(
        {
            "kAXCreatedNotification",
            "kAXWindowCreatedNotification",
            "kAXUIElementDestroyedNotification",
            "kAXValueChangedNotification",
            "kAXTitleChangedNotification",
        }
    )

    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self._ax: Any = None
        self._run_loop: Any = None

    def _run_backend_for_test(self) -> None:
        self._run_backend()

    def _run_backend(self) -> None:
        import ApplicationServices as ax
        import objc

        self._ax = ax

        @objc.callbackFor(ax.AXObserverCreate)
        def callback(observer, element, notification, refcon):
            self._signal_change()

        error, observer = ax.AXObserverCreate(self.pid, callback, None)
        if error != 0 or observer is None:
            self._mark_ready(False)
            return

        application = ax.AXUIElementCreateApplication(self.pid)
        registered: list[tuple[str, Any]] = []
        source = None
        run_loop = None
        try:
            for constant_name in self._NOTIFICATION_NAMES:
                notification = getattr(ax, constant_name, None)
                if notification is None:
                    continue
                try:
                    result = ax.AXObserverAddNotification(
                        observer, application, notification, None
                    )
                except Exception:
                    continue
                if result == 0:
                    registered.append((constant_name, notification))

            registered_names = {name for name, _notification in registered}
            coverage_complete = self._MUTATION_CRITICAL_NOTIFICATIONS.issubset(
                registered_names
            )
            if not coverage_complete:
                self._mark_ready(False)
                return

            source = ax.AXObserverGetRunLoopSource(observer)
            run_loop = ax.CFRunLoopGetCurrent()
            if source is None or run_loop is None:
                self._mark_ready(False)
                return

            self._run_loop = run_loop
            ax.CFRunLoopAddSource(run_loop, source, ax.kCFRunLoopDefaultMode)
            self._mark_ready(True)
            if not self._stop_requested.is_set():
                ax.CFRunLoopRun()
        finally:
            if run_loop is not None and source is not None:
                try:
                    ax.CFRunLoopRemoveSource(run_loop, source, ax.kCFRunLoopDefaultMode)
                except Exception:
                    pass
            for _name, notification in reversed(registered):
                try:
                    ax.AXObserverRemoveNotification(observer, application, notification)
                except Exception:
                    pass
            self._run_loop = None

    def _request_platform_stop(self) -> None:
        ax = self._ax
        run_loop = self._run_loop
        if ax is not None and run_loop is not None:
            try:
                ax.CFRunLoopStop(run_loop)
            except Exception:
                pass


class _WindowsUIARuntime:
    """Typed comtypes wrapper created and destroyed on one COM thread."""

    process_id_property = 30002
    tree_scope_children = 2
    tree_scope_descendants = 4
    tree_scope_subtree = 7
    event_ids = (20008, 20015, 20024)  # layout, text, live-region changes
    global_event_ids = (20016,)  # top-level window opened
    mutation_critical_global_event_ids = (20016,)
    # Name/value/focus/selection changes.  ProcessId is 30002; 30008 is
    # HasKeyboardFocus and 30079 is SelectionItemIsSelected.
    property_ids = (30003, 30005, 30008, 30010, 30022, 30045, 30079)

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        import comtypes
        import comtypes.client

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        self._comtypes = comtypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._closed = False

        try:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uiac

            self._uiac = uiac
            self.automation = uiac.CUIAutomation().QueryInterface(uiac.IUIAutomation)

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._create_event = kernel32.CreateEventW
            self._create_event.argtypes = [
                ctypes.c_void_p,
                wintypes.BOOL,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            self._create_event.restype = wintypes.HANDLE
            self._set_event = kernel32.SetEvent
            self._set_event.argtypes = [wintypes.HANDLE]
            self._set_event.restype = wintypes.BOOL
            self._close_handle = kernel32.CloseHandle
            self._close_handle.argtypes = [wintypes.HANDLE]
            self._close_handle.restype = wintypes.BOOL

            ole32 = ctypes.OleDLL("ole32")
            self._co_wait = ole32.CoWaitForMultipleHandles
            self._co_wait.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.ULONG,
                ctypes.POINTER(wintypes.HANDLE),
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._co_wait.restype = ctypes.c_long
            self._stop_handle = self._create_event(None, True, False, None)
            if not self._stop_handle:
                raise OSError(ctypes.get_last_error(), "CreateEventW failed")
        except Exception:
            comtypes.CoUninitialize()
            raise

    def make_structure_handler(self, callback: Callable[[], None]):
        comtypes = self._comtypes
        interface = self._uiac.IUIAutomationStructureChangedEventHandler

        class StructureHandler(comtypes.COMObject):
            _com_interfaces_ = [interface]

            def HandleStructureChangedEvent(
                self, this, sender, change_type, runtime_id
            ):
                callback()
                return 0

        return StructureHandler()

    def make_event_handler(self, callback: Callable[[], None]):
        comtypes = self._comtypes
        interface = self._uiac.IUIAutomationEventHandler

        class AutomationEventHandler(comtypes.COMObject):
            _com_interfaces_ = [interface]

            def HandleAutomationEvent(self, this, sender, event_id):
                callback(sender, event_id)
                return 0

        return AutomationEventHandler()

    def make_property_handler(self, callback: Callable[[], None]):
        comtypes = self._comtypes
        interface = self._uiac.IUIAutomationPropertyChangedEventHandler

        class PropertyChangedEventHandler(comtypes.COMObject):
            _com_interfaces_ = [interface]

            def HandlePropertyChangedEvent(self, this, sender, property_id, new_value):
                callback()
                return 0

        return PropertyChangedEventHandler()

    def make_property_array(self, property_ids: tuple[int, ...]):
        return (self._ctypes.c_int * len(property_ids))(*property_ids)

    def wait_until_stopped(self, stop_event: threading.Event) -> None:
        if stop_event.is_set():
            return
        handles = (self._wintypes.HANDLE * 1)(self._stop_handle)
        signalled_index = self._wintypes.DWORD()
        self._co_wait(0, 0xFFFFFFFF, 1, handles, self._ctypes.byref(signalled_index))

    def stop(self) -> None:
        if not self._closed:
            self._set_event(self._stop_handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_handle(self._stop_handle)
        self._comtypes.CoUninitialize()


class WindowsUIAChangeSubscription(NativeChangeSubscription):
    """Windows UI Automation structure/content event subscription."""

    def __init__(
        self,
        pid: int,
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(pid)
        self._runtime_factory = runtime_factory or _WindowsUIARuntime
        self._runtime: Any = None

    def _run_backend_for_test(self) -> None:
        self._run_backend()

    def _run_backend(self) -> None:
        runtime = self._runtime_factory()
        self._runtime = runtime
        registrations: list[tuple[str, Any, int | None, Any]] = []
        registered_global_events: set[int] = set()
        structure_handler = runtime.make_structure_handler(self._signal_change)

        def handle_event(sender, event_id) -> None:
            try:
                sender_pid = int(sender.CurrentProcessId)
            except Exception:
                return
            if sender_pid == self.pid:
                self._signal_change()

        event_handler = runtime.make_event_handler(handle_event)
        property_handler = runtime.make_property_handler(self._signal_change)
        property_array = runtime.make_property_array(runtime.property_ids)
        try:
            root = runtime.automation.GetRootElement()
            condition = runtime.automation.CreatePropertyCondition(
                runtime.process_id_property, self.pid
            )
            for event_id in runtime.global_event_ids:
                try:
                    runtime.automation.AddAutomationEventHandler(
                        event_id,
                        root,
                        runtime.tree_scope_subtree,
                        None,
                        event_handler,
                    )
                    registrations.append(("event", root, event_id, event_handler))
                    registered_global_events.add(event_id)
                except Exception:
                    continue

            collection = root.FindAll(runtime.tree_scope_children, condition)
            targets = [
                collection.GetElement(index) for index in range(collection.Length)
            ]
            if not targets:
                target = root.FindFirst(runtime.tree_scope_descendants, condition)
                if target is not None:
                    targets.append(target)

            target_coverage: list[tuple[bool, bool]] = []
            for target in targets:
                structure_registered = False
                property_registered = False
                try:
                    runtime.automation.AddStructureChangedEventHandler(
                        target,
                        runtime.tree_scope_subtree,
                        None,
                        structure_handler,
                    )
                    registrations.append(("structure", target, None, structure_handler))
                    structure_registered = True
                except Exception:
                    pass

                try:
                    runtime.automation.AddPropertyChangedEventHandlerNativeArray(
                        target,
                        runtime.tree_scope_subtree,
                        None,
                        property_handler,
                        property_array,
                        len(runtime.property_ids),
                    )
                    registrations.append(("property", target, None, property_handler))
                    property_registered = True
                except Exception:
                    pass

                for event_id in runtime.event_ids:
                    try:
                        runtime.automation.AddAutomationEventHandler(
                            event_id,
                            target,
                            runtime.tree_scope_subtree,
                            None,
                            event_handler,
                        )
                        registrations.append(("event", target, event_id, event_handler))
                    except Exception:
                        continue

                target_coverage.append(
                    (structure_registered, property_registered)
                )

            required_global_events = set(
                getattr(
                    runtime,
                    "mutation_critical_global_event_ids",
                    (20016,),
                )
            )
            coverage_complete = (
                bool(target_coverage)
                and required_global_events.issubset(registered_global_events)
                and all(
                    structure_registered and property_registered
                    for structure_registered, property_registered in target_coverage
                )
            )
            self._mark_ready(coverage_complete)
            if coverage_complete and not self._stop_requested.is_set():
                runtime.wait_until_stopped(self._stop_requested)
        finally:
            for kind, target, event_id, handler in reversed(registrations):
                try:
                    if kind == "structure":
                        runtime.automation.RemoveStructureChangedEventHandler(
                            target, handler
                        )
                    elif kind == "property":
                        runtime.automation.RemovePropertyChangedEventHandler(
                            target, handler
                        )
                    else:
                        runtime.automation.RemoveAutomationEventHandler(
                            event_id, target, handler
                        )
                except Exception:
                    pass
            runtime.close()
            self._runtime = None

    def _request_platform_stop(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:
                pass


class _LinuxATSPIRuntime:
    event_types = (
        "object:children-changed",
        "object:property-change:accessible-name",
        "object:property-change:accessible-role",
        "object:property-change:accessible-value",
        "object:state-changed:visible",
        "object:state-changed:showing",
        "object:state-changed:focused",
        "object:state-changed:selected",
        "object:visible-data-changed",
        "object:text-changed",
        "object:model-changed",
        "window:create",
        "window:activate",
        "window:deactivate",
    )
    mutation_critical_event_types = frozenset(
        {
            "object:children-changed",
            "object:property-change:accessible-name",
            "object:property-change:accessible-value",
            "window:create",
        }
    )

    def __init__(self) -> None:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, GLib

        self._atspi = Atspi
        self._glib = GLib

    def find_application(self, pid: int):
        desktop = self._atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            application = desktop.get_child_at_index(index)
            if application is not None and application.get_process_id() == pid:
                return application
        return None

    def make_listener(self, callback: Callable[..., None]):
        return self._atspi.EventListener.new(callback, None, None)

    def make_main_loop(self):
        return self._glib.MainLoop()

    def idle_add(self, callback: Callable[[], Any]):
        return self._glib.idle_add(callback)


class LinuxATSPIChangeSubscription(NativeChangeSubscription):
    """Linux AT-SPI event listener hosted on a GLib main loop."""

    def __init__(
        self,
        pid: int,
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(pid)
        self._runtime_factory = runtime_factory or _LinuxATSPIRuntime
        self._runtime: Any = None
        self._main_loop: Any = None

    def _run_backend_for_test(self) -> None:
        self._run_backend()

    def _run_backend(self) -> None:
        runtime = self._runtime_factory()
        self._runtime = runtime
        application = runtime.find_application(self.pid)
        if application is None:
            self._mark_ready(False)
            return

        def callback(*callback_args) -> None:
            if not callback_args:
                return
            event = callback_args[0]
            source = getattr(event, "source", None)
            try:
                source_pid = source.get_process_id()
            except Exception:
                return
            if source_pid == self.pid:
                self._signal_change()

        listener = runtime.make_listener(callback)
        registered: list[str] = []
        main_loop = runtime.make_main_loop()
        self._main_loop = main_loop
        try:
            for event_type in runtime.event_types:
                try:
                    if listener.register_with_app(event_type, None, application):
                        registered.append(event_type)
                except Exception:
                    continue

            required_events = set(
                getattr(
                    runtime,
                    "mutation_critical_event_types",
                    _LinuxATSPIRuntime.mutation_critical_event_types,
                )
            )
            coverage_complete = required_events.issubset(registered)
            self._mark_ready(coverage_complete)
            if coverage_complete and not self._stop_requested.is_set():
                main_loop.run()
        finally:
            for event_type in reversed(registered):
                try:
                    listener.deregister(event_type)
                except Exception:
                    pass
            self._main_loop = None
            self._runtime = None

    def _request_platform_stop(self) -> None:
        runtime = self._runtime
        main_loop = self._main_loop
        if runtime is None or main_loop is None:
            return
        try:
            runtime.idle_add(main_loop.quit)
        except Exception:
            try:
                main_loop.quit()
            except Exception:
                pass


def create_native_change_subscription(
    pid: int,
    *,
    platform: str | None = None,
) -> NativeChangeSubscription | None:
    """Create a lazy platform subscription without importing another OS API."""
    current_platform = platform or sys.platform
    if current_platform == "darwin":
        return MacOSAXChangeSubscription(pid)
    if current_platform == "win32":
        if sys.platform == "win32":
            # Import on the server thread first.  comtypes initializes the
            # importing thread automatically; the observer's dedicated thread
            # can then explicitly enter the UIA-recommended MTA below.
            import comtypes  # noqa: F401

        return WindowsUIAChangeSubscription(pid)
    if current_platform.startswith("linux"):
        return LinuxATSPIChangeSubscription(pid)
    return None


async def open_native_change_subscription(
    pid: int,
    *,
    startup_timeout: float = _EVENT_STARTUP_TIMEOUT,
) -> NativeChangeSubscription | None:
    """Register the platform observer, returning ``None`` when unavailable."""
    subscription = create_native_change_subscription(pid)
    if subscription is None:
        return None
    try:
        if await subscription.start(timeout=startup_timeout):
            return subscription
    except BaseException:
        await subscription.aclose()
        raise
    await subscription.aclose()
    return None


async def _call_subscription_factory(
    factory: SubscriptionFactory,
    pid: int,
    timeout: float,
) -> _ChangeSubscription | None:
    candidate = factory(pid)
    if not inspect.isawaitable(candidate):
        return candidate  # type: ignore[return-value]
    return await asyncio.wait_for(candidate, timeout=timeout)


async def run_native_action_until(
    pid: int,
    action: Callable[[], Any],
    condition: Callable[[], bool],
    *,
    timeout: float = 0.5,
    subscription_factory: SubscriptionFactory | None = None,
    fallback_initial_interval: float = 0.01,
    fallback_max_interval: float = 0.08,
    budget: OperationBudget | None = None,
    worker: ProviderWorker | None = None,
    action_worker: ProviderWorker | None = None,
    condition_worker: ProviderWorker | None = None,
    action_state: ProviderCallState | None = None,
    skip_action_if_condition: bool = False,
    require_subscription_for_dispatch: bool = False,
    abort_dispatch_on_change: bool = False,
    pre_dispatch_check: Callable[[], None] | None = None,
) -> NativeActionResult:
    """Run one foreground action and observe its completion without a fixed delay.

    The OS accessibility observer is registered before ``action`` runs, avoiding
    a lost notification when an operation completes quickly.  When native event
    registration is unavailable, the condition is checked with a short bounded
    backoff until the same deadline.  The action itself is never retried.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    timeout = max(0.0, float(timeout))
    operation_budget = (
        budget.child(timeout) if budget is not None else OperationBudget.start(timeout)
    )
    dispatch_worker = action_worker or worker
    observation_worker = condition_worker or worker
    subscription: _ChangeSubscription | None = None
    checks = 0
    watchdog_rechecks = 0

    if not operation_budget.expired:
        try:
            if subscription_factory is None:
                subscription = await _await_with_budget(
                    open_native_change_subscription(
                        pid,
                        startup_timeout=min(
                            _EVENT_STARTUP_TIMEOUT,
                            operation_budget.remaining(),
                        ),
                    ),
                    budget=operation_budget,
                    operation="native observer registration",
                )
            else:
                subscription = await _await_with_budget(
                    _call_subscription_factory(
                        subscription_factory,
                        pid,
                        min(_EVENT_STARTUP_TIMEOUT, operation_budget.remaining()),
                    ),
                    budget=operation_budget,
                    operation="native observer registration",
                )
        except OperationError:
            raise
        except Exception as exc:
            logger.debug(
                "Native action events unavailable for PID %s "
                "(exception_type=%s)",
                pid,
                type(exc).__name__,
            )
            subscription = None

    generation = subscription.generation if subscription is not None else 0
    try:
        async def completed() -> bool:
            nonlocal checks
            checks += 1
            return bool(
                await _run_sync_with_budget(
                    condition,
                    budget=operation_budget,
                    operation="native completion check",
                    worker=observation_worker,
                )
            )

        if skip_action_if_condition:
            if await completed():
                return NativeActionResult(
                    action_result=None,
                    condition_met=True,
                    elapsed=loop.time() - started,
                    event_driven=subscription is not None,
                    checks=checks,
                    action_dispatched=False,
                )
            if (
                abort_dispatch_on_change
                and subscription is not None
                and subscription.generation != generation
            ):
                raise OperationError(
                    OperationErrorCode.PROVIDER_BUSY,
                    "native state changed during pre-dispatch verification",
                )

        if require_subscription_for_dispatch and (
            subscription is None or not getattr(subscription, "active", True)
        ):
            raise OperationError(
                OperationErrorCode.PROVIDER_BUSY,
                "native observer coverage is required before action dispatch",
            )

        def pre_dispatch_guard() -> None:
            def verify_subscription() -> None:
                if require_subscription_for_dispatch and (
                    subscription is None or not getattr(subscription, "active", True)
                ):
                    raise OperationError(
                        OperationErrorCode.PROVIDER_BUSY,
                        "native observer coverage ended before action dispatch",
                    )
                if (
                    abort_dispatch_on_change
                    and subscription is not None
                    and subscription.generation != generation
                ):
                    raise OperationError(
                        OperationErrorCode.PROVIDER_BUSY,
                        "native state changed during pre-dispatch verification",
                    )

            verify_subscription()
            if pre_dispatch_check is not None:
                pre_dispatch_check()
            verify_subscription()

        operation_budget.checkpoint("native action dispatch")
        action_result = await _run_sync_with_budget(
            action,
            budget=operation_budget,
            operation="native action",
            worker=dispatch_worker,
            state=action_state,
            pre_dispatch=pre_dispatch_guard,
        )

        if await completed():
            return NativeActionResult(
                action_result=action_result,
                condition_met=True,
                elapsed=loop.time() - started,
                event_driven=subscription is not None,
                checks=checks,
            )

        fallback_required = subscription is None
        if subscription is not None:
            while True:
                if operation_budget.expired:
                    break
                try:
                    changed, watchdog_elapsed = await _wait_for_change_or_watchdog(
                        subscription,
                        generation,
                        budget=operation_budget,
                        operation="native completion event",
                        watchdog_rechecks=watchdog_rechecks,
                    )
                except OperationError as exc:
                    if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                        break
                    raise
                if not changed and not watchdog_elapsed:
                    fallback_required = True
                    break
                if watchdog_elapsed:
                    watchdog_rechecks += 1
                generation = subscription.generation
                try:
                    is_complete = await completed()
                except OperationError as exc:
                    if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                        break
                    raise
                if is_complete:
                    return NativeActionResult(
                        action_result=action_result,
                        condition_met=True,
                        elapsed=loop.time() - started,
                        event_driven=True,
                        checks=checks,
                    )

        if fallback_required:
            interval = max(0.001, float(fallback_initial_interval))
            maximum = max(interval, float(fallback_max_interval))
            while checks < _MAX_FALLBACK_CHECKS:
                remaining = operation_budget.remaining()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(interval, remaining))
                if operation_budget.expired:
                    break
                try:
                    is_complete = await completed()
                except OperationError as exc:
                    if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                        break
                    raise
                if is_complete:
                    return NativeActionResult(
                        action_result=action_result,
                        condition_met=True,
                        elapsed=loop.time() - started,
                        event_driven=False,
                        checks=checks,
                    )
                interval = min(maximum, interval * _FALLBACK_GROWTH)

        return NativeActionResult(
            action_result=action_result,
            condition_met=False,
            elapsed=loop.time() - started,
            event_driven=subscription is not None and not fallback_required,
            checks=checks,
        )
    finally:
        if subscription is not None:
            await _close_with_budget(subscription, budget=operation_budget)


async def run_native_action_until_any(
    pids: tuple[int, ...],
    action: Callable[[], Any],
    condition: Callable[[], bool],
    *,
    timeout: float = 0.5,
    subscription_factory: SubscriptionFactory | None = None,
    fallback_initial_interval: float = 0.01,
    fallback_max_interval: float = 0.08,
    budget: OperationBudget | None = None,
    action_worker: ProviderWorker | None = None,
    condition_worker: ProviderWorker | None = None,
    action_state: ProviderCallState | None = None,
    skip_action_if_condition: bool = False,
    require_all_subscriptions: bool = False,
    abort_dispatch_on_change: bool = False,
    pre_dispatch_check: Callable[[], None] | None = None,
) -> NativeActionResult:
    """Observe one action across all current candidate application processes."""
    unique_pids = tuple(
        dict.fromkeys(
            pid
            for pid in pids
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        )
    )

    async def open_composite(_pid: int) -> _ChangeSubscription | None:
        if not unique_pids:
            return None
        factory = subscription_factory

        async def open_one(pid: int) -> _ChangeSubscription | None:
            if factory is None:
                return await open_native_change_subscription(pid)
            return await _call_subscription_factory(factory, pid, timeout)

        opened = await asyncio.gather(
            *(open_one(pid) for pid in unique_pids),
            return_exceptions=True,
        )
        subscriptions = tuple(
            candidate
            for candidate in opened
            if candidate is not None and not isinstance(candidate, BaseException)
        )
        if require_all_subscriptions and len(subscriptions) != len(unique_pids):
            await asyncio.gather(
                *(subscription.aclose() for subscription in subscriptions),
                return_exceptions=True,
            )
            return None
        if not subscriptions:
            return None
        return _CompositeChangeSubscription(subscriptions)

    return await run_native_action_until(
        unique_pids[0] if unique_pids else 0,
        action,
        condition,
        timeout=timeout,
        subscription_factory=open_composite,
        fallback_initial_interval=fallback_initial_interval,
        fallback_max_interval=fallback_max_interval,
        budget=budget,
        action_worker=action_worker,
        condition_worker=condition_worker,
        action_state=action_state,
        skip_action_if_condition=skip_action_if_condition,
        require_subscription_for_dispatch=(
            require_all_subscriptions and bool(unique_pids)
        ),
        abort_dispatch_on_change=abort_dispatch_on_change,
        pre_dispatch_check=pre_dispatch_check,
    )


async def wait_for_native_element(
    adapter: Any,
    pid: int,
    *,
    role: str = "",
    name: str = "",
    timeout: float = 5.0,
    subscription_factory: SubscriptionFactory | None = None,
    fallback_initial_interval: float = _FALLBACK_INITIAL_INTERVAL,
    fallback_max_interval: float = _FALLBACK_MAX_INTERVAL,
    budget: OperationBudget | None = None,
    worker: ProviderWorker | None = None,
) -> NativeWaitResult:
    """Wait for one matching foreground element with native notifications.

    Registration happens before the first accessibility query.  When native
    registration is unavailable, the fallback starts quickly, then backs off
    to a bounded interval to avoid hammering an application's accessibility
    process.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    timeout = max(0.0, float(timeout))
    operation_budget = (
        budget.child(timeout) if budget is not None else OperationBudget.start(timeout)
    )
    subscription: _ChangeSubscription | None = None
    checks = 0
    watchdog_rechecks = 0

    async def find_first() -> UIElement | None:
        nonlocal checks
        checks += 1
        matches = await _run_sync_with_budget(
            lambda: adapter.find_elements(pid, role=role, name=name),
            budget=operation_budget,
            operation="native element query",
            worker=worker,
        )
        return matches[0] if matches else None

    if not operation_budget.expired:
        try:
            if subscription_factory is None:
                subscription = await _await_with_budget(
                    open_native_change_subscription(
                        pid,
                        startup_timeout=min(
                            _EVENT_STARTUP_TIMEOUT,
                            operation_budget.remaining(),
                        ),
                    ),
                    budget=operation_budget,
                    operation="native observer registration",
                )
            else:
                subscription = await _await_with_budget(
                    _call_subscription_factory(
                        subscription_factory,
                        pid,
                        operation_budget.remaining(),
                    ),
                    budget=operation_budget,
                    operation="native observer registration",
                )
        except OperationError as exc:
            if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                return NativeWaitResult(
                    element=None,
                    elapsed=loop.time() - started,
                    event_driven=False,
                    checks=checks,
                )
            raise
        except Exception as exc:
            logger.debug(
                "Native events unavailable for PID %s (exception_type=%s)",
                pid,
                type(exc).__name__,
            )
            subscription = None

    if subscription is not None:
        try:
            generation = subscription.generation
            try:
                element = await find_first()
            except OperationError as exc:
                if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                    return NativeWaitResult(
                        element=None,
                        elapsed=loop.time() - started,
                        event_driven=True,
                        checks=checks,
                    )
                raise
            if element is not None:
                return NativeWaitResult(
                    element=element,
                    elapsed=loop.time() - started,
                    event_driven=True,
                    checks=checks,
                )

            while True:
                if operation_budget.expired:
                    return NativeWaitResult(
                        element=None,
                        elapsed=loop.time() - started,
                        event_driven=True,
                        checks=checks,
                    )
                try:
                    changed, watchdog_elapsed = await _wait_for_change_or_watchdog(
                        subscription,
                        generation,
                        budget=operation_budget,
                        operation="native element event",
                        watchdog_rechecks=watchdog_rechecks,
                    )
                except OperationError as exc:
                    if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                        return NativeWaitResult(
                            element=None,
                            elapsed=loop.time() - started,
                            event_driven=True,
                            checks=checks,
                        )
                    raise
                if not changed and not watchdog_elapsed:
                    break
                if watchdog_elapsed:
                    watchdog_rechecks += 1
                generation = subscription.generation
                try:
                    element = await find_first()
                except OperationError as exc:
                    if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                        return NativeWaitResult(
                            element=None,
                            elapsed=loop.time() - started,
                            event_driven=True,
                            checks=checks,
                        )
                    raise
                if element is not None:
                    return NativeWaitResult(
                        element=element,
                        elapsed=loop.time() - started,
                        event_driven=True,
                        checks=checks,
                    )
        finally:
            await _close_with_budget(subscription, budget=operation_budget)

    if operation_budget.expired:
        return NativeWaitResult(
            element=None,
            elapsed=loop.time() - started,
            event_driven=False,
            checks=checks,
        )

    try:
        element = await find_first()
    except OperationError as exc:
        if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
            return NativeWaitResult(
                element=None,
                elapsed=loop.time() - started,
                event_driven=False,
                checks=checks,
            )
        raise
    if element is not None:
        return NativeWaitResult(
            element=element,
            elapsed=loop.time() - started,
            event_driven=False,
            checks=checks,
        )

    initial = max(0.001, float(fallback_initial_interval))
    maximum = max(initial, float(fallback_max_interval))
    interval = initial

    while checks < _MAX_FALLBACK_CHECKS:
        remaining = operation_budget.remaining()
        if remaining <= 0:
            break
        delay = min(interval, remaining)
        await asyncio.sleep(delay)
        if operation_budget.expired:
            break

        try:
            element = await find_first()
        except OperationError as exc:
            if exc.code is OperationErrorCode.DEADLINE_EXCEEDED:
                break
            raise
        if element is not None:
            return NativeWaitResult(
                element=element,
                elapsed=loop.time() - started,
                event_driven=False,
                checks=checks,
            )
        interval = min(maximum, interval * _FALLBACK_GROWTH)

    return NativeWaitResult(
        element=None,
        elapsed=loop.time() - started,
        event_driven=False,
        checks=checks,
    )
