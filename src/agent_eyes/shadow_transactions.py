"""Persistent-CDP ports for one explicit shadow transaction.

This integration intentionally owns no coordinator and calls no public MCP handler.
The caller selects and serializes the exact shadow target before running the pure
transaction engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import math
from typing import Any, Protocol

from .action_kernel import ActionDispatchResult, ActionPorts
from .adapters.base import UIElement
from .cdp_runtime import (
    CLICK_FUNCTION,
    RuntimeActionStatus,
    ax_element_has_exact_focus,
    ax_element_semantics_match,
    parse_runtime_action_status,
    require_empty_command_result,
)
from .locators import LocatorIndex
from .observations import ElementRecord, ObservationStore
from .operation import (
    OperationBudget,
    OperationError,
    OperationErrorCode,
    OperationMode,
)
from .target_resolver import ResolutionSource, TargetResolution
from .transaction_contract import (
    ExecuteRequest,
    Locator,
    TargetMode,
    TargetSpec,
    TransactionOperation,
    TransactionStep,
)
from .transactions import TransactionPorts, TransactionTarget, TransactionView


_PROVIDER = "cdp-persistent"
_MAX_OBSERVATION_ATTEMPTS = 2
_MAX_COMPLETION_REFRESHES = 8
_MAX_SNAPSHOT_ELEMENTS = 500
_COMPLETION_EVENTS = (
    "Accessibility.nodesUpdated",
    "Accessibility.loadComplete",
    "DOM.documentUpdated",
)
_SUPPORTED_ACTIONS = frozenset(
    {
        TransactionOperation.HOVER,
        TransactionOperation.CLICK,
        TransactionOperation.TYPE,
        TransactionOperation.PRESS_KEY,
        TransactionOperation.SCROLL,
    }
)


class ShadowTargetResolver(Protocol):
    """Injected exact-target resolver used by the transaction boundary."""

    def __call__(
        self,
        spec: TargetSpec,
        *,
        activate: bool,
        budget: OperationBudget,
    ) -> Awaitable[TargetResolution]: ...


SessionLookup = Callable[[str], Any | None]


class _RetrySafeDispatchFailure(RuntimeError):
    """A selected dispatch proved that its primary mutation was not sent."""

    def __init__(self, code: OperationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


async def _document_revision(session: Any, budget: OperationBudget) -> int:
    """Read one deterministic top-level document identity."""
    frame_result = await budget.wait_for(
        session.send("Page.getFrameTree", idempotent=True),
        operation="shadow document frame revision",
    )
    document_result = await budget.wait_for(
        session.send(
            "DOM.getDocument",
            {"depth": 0, "pierce": False},
            idempotent=True,
        ),
        operation="shadow document root revision",
    )
    frame = frame_result.get("frameTree", {}).get("frame", {})
    frame_identity = frame.get("loaderId") or frame.get("id")
    root_identity = document_result.get("root", {}).get("backendNodeId")
    if (
        not isinstance(frame_identity, str)
        or not frame_identity
        or isinstance(root_identity, bool)
        or not isinstance(root_identity, int)
        or root_identity <= 0
    ):
        raise OperationError(
            OperationErrorCode.PROVIDER_BUSY,
            "persistent CDP did not expose a stable document revision",
        )
    identity = f"{frame_identity}:{root_identity}"
    digest = hashlib.blake2b(identity.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _snapshot_records(index: LocatorIndex) -> tuple[ElementRecord, ...]:
    elements = index.elements[:_MAX_SNAPSHOT_ELEMENTS]
    return tuple(
        ElementRecord(
            local_id=element.id,
            value=element,
            actionable=(
                element.source in {"cdp", "shadow-dom"}
                and isinstance(element.platform_ref, int)
                and not isinstance(element.platform_ref, bool)
                and element.platform_ref > 0
            ),
        )
        for element in elements
    )


class PersistentCDPTransactionRuntime:
    """Bind transaction ports to one exact current-generation CDP session."""

    def __init__(
        self,
        request: ExecuteRequest,
        *,
        resolver: ShadowTargetResolver,
        session_for_target: SessionLookup,
        observation_store: ObservationStore,
        cdp_client: Any,
    ) -> None:
        if not isinstance(request, ExecuteRequest):
            raise ValueError("request must be a validated ExecuteRequest")
        if request.target.mode is not TargetMode.SHADOW:
            raise ValueError("persistent CDP runtime requires shadow mode")
        if not callable(resolver) or not callable(session_for_target):
            raise ValueError("resolver and session lookup must be callable")
        if not isinstance(observation_store, ObservationStore):
            raise ValueError("observation_store must be an ObservationStore")

        self._request = request
        self._resolver = resolver
        self._session_for_target = session_for_target
        self._observations = observation_store
        self._cdp_client = cdp_client
        self._resolution: TargetResolution | None = None
        self._session: Any | None = None
        self._generation = -1
        self._revision = -1
        self._pending_view: TransactionView | None = None
        self._completion_locators = {
            step.index: self._completion_locator(step.index) for step in request.steps
        }

    def ports(self) -> TransactionPorts:
        """Expose the same provider-neutral ports consumed by TransactionEngine."""
        return TransactionPorts(
            resolve=self.resolve,
            observe=self.observe,
            refresh=self.refresh,
            action_ports=self.action_ports,
        )

    def pending_dispatch_recovery(self) -> Awaitable[Any] | None:
        """Return settlement work for a command left pending by cancellation."""
        session = self._session
        if session is None or not bool(getattr(session, "has_pending_commands", False)):
            return None
        waiter = getattr(session, "wait_until_idle", None)
        return waiter() if callable(waiter) else None

    def _completion_locator(self, step_index: int) -> Locator | None:
        step = self._request.steps[step_index]
        if step.expect is not None:
            return step.expect
        for later in self._request.steps[step_index + 1 :]:
            if later.locator is not None:
                return later.locator
        return self._request.final_expect

    async def resolve(
        self,
        spec: TargetSpec,
        activate: bool,
        budget: OperationBudget,
        /,
    ) -> TransactionTarget:
        """Resolve an explicit persistent target without foreground activation."""
        if activate or spec.mode is not TargetMode.SHADOW:
            raise OperationError(
                OperationErrorCode.MODE_MISMATCH,
                "persistent CDP transactions require explicit shadow mode",
            )
        if not spec.target_id:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_TARGET,
                "persistent CDP transactions require an exact target ID",
            )

        resolution = await self._resolver(spec, activate=False, budget=budget)
        resolved = resolution.target
        provider_target = resolved.provider_target
        if (
            resolved.mode is not TargetMode.SHADOW
            or resolved.source is not ResolutionSource.EXACT
            or resolved.target_id != spec.target_id
            or provider_target is None
            or provider_target.target_id != resolved.target_id
        ):
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "shadow target resolution did not preserve the exact target",
            )

        binding = provider_target.value
        provider_name = (
            binding[0] if isinstance(binding, tuple) and len(binding) == 2 else ""
        )
        provider_tab = (
            binding[1] if isinstance(binding, tuple) and len(binding) == 2 else None
        )
        if provider_name != "persistent":
            raise OperationError(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                "shadow transactions require the persistent CDP provider",
            )
        if getattr(provider_tab, "id", None) != resolved.target_id:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "persistent CDP inventory target does not match the resolution",
            )

        session = self._session_for_target(resolved.target_id)
        if session is None:
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "persistent CDP target has no current session",
            )
        generation = getattr(session, "generation", None)
        if (
            getattr(session, "target_id", None) != resolved.target_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "persistent CDP session does not match the exact target",
            )

        self._resolution = resolution
        self._session = session
        self._generation = generation
        return TransactionTarget(target_id=resolved.target_id, value=resolution)

    async def observe(
        self,
        target: TransactionTarget,
        budget: OperationBudget,
        /,
    ) -> TransactionView:
        """Capture one revision-bracketed local AX observation."""
        self._require_target(target)
        snapshot_token = self._request.target.snapshot
        if snapshot_token:
            try:
                self._observations.get_snapshot(
                    snapshot_token,
                    expected_provider=_PROVIDER,
                    expected_mode=OperationMode.SHADOW,
                    expected_target_id=self._target_id,
                    expected_generation=self._generation,
                )
            except OperationError as exc:
                if exc.code is not OperationErrorCode.STALE_SNAPSHOT:
                    raise
        return await self._read_view(budget)

    async def refresh(
        self,
        target: TransactionTarget,
        current: TransactionView,
        locator: Locator,
        budget: OperationBudget,
        /,
    ) -> TransactionView:
        """Return the event-confirmed view or refresh only the selected target."""
        self._require_target(target)
        if not isinstance(current, TransactionView) or not isinstance(locator, Locator):
            raise OperationError(
                OperationErrorCode.INVALID_TRANSACTION,
                "shadow refresh received invalid transaction state",
            )
        if self._pending_view is not None:
            pending = self._pending_view
            self._pending_view = None
            return pending
        return await self._read_view(budget)

    def action_ports(
        self,
        step: TransactionStep,
        element: UIElement | None,
        target: TransactionTarget,
        /,
    ) -> ActionPorts:
        """Select exactly one persistent-CDP action implementation."""
        self._require_target(target)
        object_id: str | None = None
        hover_point: tuple[int, int] | None = None

        async def capability(budget: OperationBudget) -> bool:
            nonlocal hover_point, object_id
            if step.operation not in _SUPPORTED_ACTIONS:
                return False
            session = self._require_current_session()
            if step.operation is not TransactionOperation.SCROLL and element is None:
                return False
            await budget.wait_for(
                session.enable_domain("Accessibility"),
                operation="shadow action accessibility setup",
            )
            await budget.wait_for(
                session.enable_domain("DOM"),
                operation="shadow action DOM setup",
            )
            revision = await _document_revision(session, budget)
            self._require_current_session()
            if self._revision < 0:
                # A target-level scroll can be the first step and needs no AX scan.
                self._revision = revision
            elif revision != self._revision:
                raise OperationError(
                    OperationErrorCode.STALE_SNAPSHOT,
                    "shadow document changed before action dispatch",
                )
            if step.operation is TransactionOperation.HOVER and element is not None:
                hover_point = await self._resolve_hover_point(element, session, budget)
                geometry_revision = await _document_revision(session, budget)
                self._require_current_session()
                if geometry_revision != self._revision:
                    raise OperationError(
                        OperationErrorCode.STALE_SNAPSHOT,
                        "shadow document changed while hover geometry was resolved",
                    )
                await self._assert_element_semantics(element, session, budget)
            elif (
                element is not None
                and step.operation is not TransactionOperation.SCROLL
            ):
                object_id = await self._resolve_object(element, session, budget)
                await self._assert_element_semantics(element, session, budget)
            return True

        async def dispatch(budget: OperationBudget) -> ActionDispatchResult:
            session = self._require_current_session()
            changed = asyncio.Event()

            def wake(_params: dict[str, Any]) -> None:
                changed.set()

            for method in _COMPLETION_EVENTS:
                session.on_event(method, wake)
            try:
                await self._perform_action(
                    step,
                    element,
                    object_id,
                    hover_point,
                    session,
                    budget,
                )
                self._invalidate_target()
                completion = self._completion_locators.get(step.index)
                if completion is not None:
                    self._pending_view = await self._await_completion(
                        completion,
                        changed,
                        budget,
                    )
                return ActionDispatchResult.succeeded(changed=True)
            except _RetrySafeDispatchFailure as exc:
                self._invalidate_target()
                return ActionDispatchResult.failed(exc.code, retry_safe=True)
            except asyncio.CancelledError:
                self._invalidate_target()
                raise
            except Exception:
                self._invalidate_target()
                return ActionDispatchResult.failed(OperationErrorCode.OUTCOME_UNKNOWN)
            finally:
                for method in _COMPLETION_EVENTS:
                    session.off_event(method, wake)

        return ActionPorts(
            provider_code="shadow.cdp",
            capability=capability,
            dispatch=dispatch,
        )

    async def _resolve_object(
        self,
        element: UIElement,
        session: Any,
        budget: OperationBudget,
    ) -> str:
        backend_id = self._backend_id(element)
        result = await budget.wait_for(
            session.send(
                "DOM.resolveNode",
                {"backendNodeId": backend_id},
                idempotent=True,
            ),
            operation="shadow action node resolution",
        )
        object_id = result.get("object", {}).get("objectId")
        if not isinstance(object_id, str) or not object_id:
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "shadow action node is no longer available",
            )
        return object_id

    async def _assert_element_semantics(
        self,
        element: UIElement,
        session: Any,
        budget: OperationBudget,
    ) -> None:
        backend_id = self._backend_id(element)
        result = await budget.wait_for(
            session.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_id, "fetchRelatives": False},
                idempotent=True,
            ),
            operation="shadow action AX identity validation",
        )
        if not ax_element_semantics_match(
            result,
            backend_node_id=backend_id,
            expected_role=element.role,
            expected_name=element.name,
        ):
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "shadow action element semantics changed",
            )

    async def _resolve_hover_point(
        self,
        element: UIElement,
        session: Any,
        budget: OperationBudget,
    ) -> tuple[int, int]:
        backend_id = self._backend_id(element)
        result = await budget.wait_for(
            session.send(
                "DOM.getBoxModel",
                {"backendNodeId": backend_id},
                idempotent=True,
            ),
            operation="shadow hover geometry",
        )
        model = result.get("model") if isinstance(result, dict) else None
        if not isinstance(model, dict):
            raise OperationError(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                "shadow hover target did not expose box geometry",
            )
        for name in ("content", "border"):
            center = self._quad_center(model.get(name))
            if center is not None:
                return center
        raise OperationError(
            OperationErrorCode.UNSUPPORTED_CAPABILITY,
            "shadow hover target did not expose usable box geometry",
        )

    @staticmethod
    def _backend_id(element: UIElement) -> int:
        backend_id = element.platform_ref
        if (
            isinstance(backend_id, bool)
            or not isinstance(backend_id, int)
            or backend_id <= 0
        ):
            raise OperationError(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                "shadow action requires an actionable backend DOM node",
            )
        return backend_id

    @staticmethod
    def _quad_center(value: object) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 8:
            return None
        coordinates = value[:8]
        if any(
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(float(coordinate))
            for coordinate in coordinates
        ):
            return None
        xs = [float(coordinates[index]) for index in range(0, 8, 2)]
        ys = [float(coordinates[index]) for index in range(1, 8, 2)]
        if max(xs) <= min(xs) or max(ys) <= min(ys):
            return None
        return int(sum(xs) / 4), int(sum(ys) / 4)

    async def _perform_action(
        self,
        step: TransactionStep,
        element: UIElement | None,
        object_id: str | None,
        hover_point: tuple[int, int] | None,
        session: Any,
        budget: OperationBudget,
    ) -> None:
        budget.checkpoint("shadow provider dispatch")
        if step.operation is TransactionOperation.HOVER:
            if hover_point is None:
                raise _RetrySafeDispatchFailure(
                    OperationErrorCode.UNSUPPORTED_CAPABILITY
                )
            await session.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": hover_point[0],
                    "y": hover_point[1],
                },
            )
            return

        if step.operation is TransactionOperation.CLICK:
            result = await session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": CLICK_FUNCTION,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            status = parse_runtime_action_status(
                result,
                allowed=frozenset({RuntimeActionStatus.CLICK_APPLIED}),
            )
            if status is not RuntimeActionStatus.CLICK_APPLIED:
                raise AssertionError("unreachable click action status")
            return

        if step.operation in {
            TransactionOperation.TYPE,
            TransactionOperation.PRESS_KEY,
        }:
            if element is None:
                raise RuntimeError("shadow input action omitted its exact element")
            backend_id = self._backend_id(element)
            focus_result = await budget.wait_for(
                session.send(
                    "DOM.focus",
                    {"backendNodeId": backend_id},
                ),
                operation="shadow element protocol focus",
            )
            require_empty_command_result(focus_result)
            focused_result = await budget.wait_for(
                session.send(
                    "Accessibility.getPartialAXTree",
                    {"backendNodeId": backend_id, "fetchRelatives": False},
                    idempotent=True,
                ),
                operation="shadow element AX focus verification",
            )
            if not ax_element_has_exact_focus(
                focused_result,
                backend_node_id=backend_id,
                expected_role=element.role,
                expected_name=element.name,
            ):
                raise _RetrySafeDispatchFailure(OperationErrorCode.FOCUS_MISMATCH)
            if step.operation is TransactionOperation.TYPE:
                await session.send("Input.insertText", {"text": step.text})
            else:
                await session.press_key(step.key)
            return

        if step.operation is TransactionOperation.SCROLL:
            x, y = self._scroll_point(element)
            await session.scroll(x, y, step.delta_x, step.delta_y)
            return

        raise OperationError(
            OperationErrorCode.UNSUPPORTED_CAPABILITY,
            "persistent CDP action is unsupported",
        )

    @staticmethod
    def _scroll_point(element: UIElement | None) -> tuple[int, int]:
        if element is None or element.bounds is None:
            return 400, 400
        x, y, width, height = element.bounds
        return int(x + width / 2), int(y + height / 2)

    async def _await_completion(
        self,
        locator: Locator,
        changed: asyncio.Event,
        budget: OperationBudget,
    ) -> TransactionView:
        view = await self._read_view(budget)
        if self._completion_ready(view, locator, event_observed=False):
            return view

        for _ in range(_MAX_COMPLETION_REFRESHES):
            if not changed.is_set():
                await budget.wait_for(
                    changed.wait(),
                    operation="shadow action completion event",
                )
            changed.clear()
            view = await self._read_view(budget)
            if self._completion_ready(view, locator, event_observed=True):
                return view
        raise OperationError(
            OperationErrorCode.OUTCOME_UNKNOWN,
            "shadow action completion was not observed within the refresh bound",
        )

    @staticmethod
    def _completion_ready(
        view: TransactionView,
        locator: Locator,
        *,
        event_observed: bool,
    ) -> bool:
        if locator.within:
            # The pure engine owns alias reconstruction. An observed provider event
            # supplies the refreshed view; the engine still verifies the scoped match.
            return event_observed
        matches = view.index.find(locator)
        if len(matches) > 1:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_ELEMENT,
                "completion locator matched multiple shadow elements",
            )
        return len(matches) == 1

    async def _read_view(self, budget: OperationBudget) -> TransactionView:
        session = self._require_current_session()
        await budget.wait_for(
            session.enable_domain("Accessibility"),
            operation="shadow accessibility setup",
        )
        await budget.wait_for(
            session.enable_domain("DOM"),
            operation="shadow DOM setup",
        )

        for attempt in range(_MAX_OBSERVATION_ATTEMPTS):
            revision_before = await _document_revision(session, budget)
            result = await budget.wait_for(
                session.send(
                    "Accessibility.getFullAXTree",
                    {"depth": 10},
                    idempotent=True,
                ),
                operation="shadow accessibility observation",
            )
            nodes = result.get("nodes", [])
            if not isinstance(nodes, list):
                raise OperationError(
                    OperationErrorCode.PROVIDER_BUSY,
                    "persistent CDP returned an invalid accessibility observation",
                )

            async def secure_send(method: str, params: dict) -> dict:
                return await session.send(method, params, idempotent=True)

            secure_metadata = await budget.wait_for(
                self._cdp_client.collect_secure_dom_metadata(secure_send, nodes),
                operation="shadow secure-field classification",
            )
            root = self._cdp_client._build_tree(
                nodes,
                secure_metadata=secure_metadata,
            )
            revision_after = await _document_revision(session, budget)
            self._require_current_session()
            if revision_before != revision_after:
                if attempt + 1 < _MAX_OBSERVATION_ATTEMPTS:
                    continue
                raise OperationError(
                    OperationErrorCode.STALE_SNAPSHOT,
                    "shadow document changed while it was observed",
                )

            index = (
                LocatorIndex.from_roots(root) if root is not None else LocatorIndex(())
            )
            records = _snapshot_records(index)
            self._observations.invalidate_target(
                provider=_PROVIDER,
                mode=OperationMode.SHADOW,
                target_id=self._target_id,
            )
            snapshot = self._observations.create(
                provider=_PROVIDER,
                mode=OperationMode.SHADOW,
                target_id=self._target_id,
                generation=self._generation,
                revision=revision_after,
                elements=records,
                detach_ui_trees=True,
                truncated=len(records) < len(index.elements),
            )
            self._revision = revision_after
            return TransactionView(index=index, snapshot=snapshot.token)

        raise AssertionError("bounded shadow observation loop did not terminate")

    @property
    def _target_id(self) -> str:
        resolution = self._resolution
        if resolution is None:
            return ""
        return resolution.target.target_id

    def _require_target(self, target: TransactionTarget) -> None:
        if self._resolution is None or target.target_id != self._target_id:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "transaction target does not match the persistent CDP binding",
            )

    def _require_current_session(self) -> Any:
        session = self._session
        if session is None or not self._target_id:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "persistent CDP transaction target is unresolved",
            )
        current = self._session_for_target(self._target_id)
        if (
            current is not session
            or getattr(current, "target_id", None) != self._target_id
            or getattr(current, "generation", None) != self._generation
        ):
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "persistent CDP target connection changed",
            )
        return session

    def _invalidate_target(self) -> None:
        if not self._target_id:
            return
        self._pending_view = None
        self._observations.invalidate_target(
            provider=_PROVIDER,
            mode=OperationMode.SHADOW,
            target_id=self._target_id,
        )
