"""Compact, server-independent observations for one exact UI target."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
import json
import time
from typing import Protocol, TypeAlias

from .adapters.base import UIElement
from .locators import LocatorIndex
from .observations import ElementRecord, ObservationSnapshot, ObservationStore
from .operation import (
    OperationBudget,
    OperationError,
    OperationErrorCode,
    OperationMode,
)
from .target_resolver import TargetResolution
from .transaction_contract import ObserveTargetRequest, TargetIntent, TargetSpec


OBSERVE_TARGET_RESULT_BYTES = 4 * 1024
DEFAULT_OBSERVATION_DEPTH = 10
MAX_OBSERVATION_DEPTH = 20
MAX_SNAPSHOT_ELEMENTS = 500

_MAX_ROLE_BYTES = 64
_MAX_NAME_BYTES = 192
_MAX_VALUE_BYTES = 128
_MAX_STATE_BYTES = 32
_MAX_ACTION_BYTES = 32
_MAX_STATES = 4
_MAX_ACTIONS = 4
_MIN_RESULT_BYTES = 256


class ObservationLoadKind(Enum):
    """The single provider scan selected for an exact target."""

    TREE = "tree"
    SUBTREE = "subtree"


class SelectorMatchStatus(Enum):
    """Whether a discovery selector is safe to reuse without arbitrary choice."""

    MISSING = "missing"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CompactElement:
    """Bounded public metadata for one explicitly matched UI element."""

    local_id: int
    role: str
    name: str = ""
    value: str = ""
    states: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"id": self.local_id, "role": self.role}
        if self.name:
            payload["name"] = self.name
        if self.value:
            payload["value"] = self.value
        if self.states:
            payload["states"] = list(self.states)
        if self.actions:
            payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True, slots=True)
class SelectorMatches:
    """All bounded candidates for one selector, without choosing ambiguities."""

    index: int
    status: SelectorMatchStatus
    total: int
    truncated: bool
    matches: tuple[CompactElement, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "status": self.status.value,
            "total": self.total,
            "truncated": self.truncated,
            "matches": [match.to_dict() for match in self.matches],
        }


@dataclass(frozen=True, slots=True)
class ObservedTarget:
    """Redacted identity and activation state for the resolved target."""

    target_id: str
    mode: str
    source: str
    pid: int | None
    activated: bool

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.target_id,
            "mode": self.mode,
            "source": self.source,
            "activated": self.activated,
        }
        if self.pid is not None:
            payload["pid"] = self.pid
        return payload


@dataclass(frozen=True, slots=True)
class ObservationScan:
    """Compact scan and short inventory-cache metadata."""

    kind: ObservationLoadKind
    nodes: int
    available_nodes: int
    truncated: bool
    inventory_cache: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "nodes": self.nodes,
            "available_nodes": self.available_nodes,
            "truncated": self.truncated,
            "inventory_cache": self.inventory_cache,
        }


@dataclass(frozen=True, slots=True)
class TargetObservationResult:
    """One immutable snapshot, locator index, and compact discovery result."""

    target: ObservedTarget
    snapshot: ObservationSnapshot
    scan: ObservationScan
    selectors: tuple[SelectorMatches, ...]
    index: LocatorIndex = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "target": self.target.to_dict(),
            "snapshot": {
                "token": self.snapshot.token,
                "provider": self.snapshot.provider,
                "generation": self.snapshot.generation,
                "revision": self.snapshot.revision,
            },
            "scan": self.scan.to_dict(),
            "selectors": [selector.to_dict() for selector in self.selectors],
            "truncated": self.scan.truncated
            or any(selector.truncated for selector in self.selectors),
        }

    def to_json(self, *, byte_limit: int = OBSERVE_TARGET_RESULT_BYTES) -> str:
        """Render valid compact JSON, removing only trailing match rows if needed."""
        if (
            isinstance(byte_limit, bool)
            or not isinstance(byte_limit, int)
            or byte_limit < _MIN_RESULT_BYTES
        ):
            raise ValueError(f"byte_limit must be an integer >= {_MIN_RESULT_BYTES}")

        selector_payloads = [selector.to_dict() for selector in self.selectors]
        payload = self.to_dict()
        payload["selectors"] = selector_payloads
        rendered = _encode(payload)
        if len(rendered.encode("utf-8")) <= byte_limit:
            return rendered

        payload["truncated"] = True
        while len(rendered.encode("utf-8")) > byte_limit:
            populated = [
                (selector, matches)
                for selector in selector_payloads
                if isinstance(
                    (matches := selector.get("matches")),
                    list,
                )
                and matches
            ]
            if not populated:
                break
            selector, matches = max(
                populated,
                key=lambda candidate: len(candidate[1]),
            )
            matches.pop()
            selector["truncated"] = True
            rendered = _encode(payload)

        if len(rendered.encode("utf-8")) <= byte_limit:
            return rendered

        # The normal bounded fields fit comfortably within 4 KiB. Keep a final
        # valid-JSON fail-safe for unusually long injected provider metadata.
        fallback = {
            "status": "ok",
            "target": {
                "id": _compact_text(self.target.target_id, 128),
                "mode": self.target.mode,
                "activated": self.target.activated,
            },
            "snapshot": {"token": _compact_text(self.snapshot.token, 64)},
            "selectors": [],
            "truncated": True,
        }
        rendered = _encode(fallback)
        if len(rendered.encode("utf-8")) > byte_limit:
            raise OperationError(
                OperationErrorCode.RESULT_TRUNCATED,
                "compact observation metadata exceeds the result limit",
            )
        return rendered


class TargetResolverPort(Protocol):
    """The resolver boundary needed by compact target observation."""

    async def resolve(
        self,
        spec: TargetSpec,
        *,
        provider_identity: object,
        adapter_identity: object,
        activate: bool = False,
        budget: OperationBudget | None = None,
    ) -> TargetResolution: ...


TreeLoader: TypeAlias = Callable[
    [object, int, int, OperationBudget], Awaitable[UIElement | None]
]
SubtreeLoader: TypeAlias = Callable[
    [object, UIElement, int, OperationBudget], Awaitable[UIElement | None]
]
TargetValidator: TypeAlias = Callable[
    [object, TargetResolution, OperationBudget], Awaitable[bool]
]


class TargetObservationService:
    """Resolve, optionally activate, and scan one target exactly once."""

    def __init__(
        self,
        resolver: TargetResolverPort,
        store: ObservationStore,
        *,
        tree_loader: TreeLoader,
        subtree_loader: SubtreeLoader,
        provider: str,
        target_validator: TargetValidator | None = None,
        revision_factory: Callable[[], int] = time.monotonic_ns,
        max_depth: int = DEFAULT_OBSERVATION_DEPTH,
        max_snapshot_elements: int = MAX_SNAPSHOT_ELEMENTS,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if (
            isinstance(max_depth, bool)
            or not isinstance(max_depth, int)
            or not 1 <= max_depth <= MAX_OBSERVATION_DEPTH
        ):
            raise ValueError(
                f"max_depth must be an integer from 1 to {MAX_OBSERVATION_DEPTH}"
            )
        if (
            isinstance(max_snapshot_elements, bool)
            or not isinstance(max_snapshot_elements, int)
            or not 1 <= max_snapshot_elements <= MAX_SNAPSHOT_ELEMENTS
        ):
            raise ValueError(
                "max_snapshot_elements must be an integer from "
                f"1 to {MAX_SNAPSHOT_ELEMENTS}"
            )
        self._resolver = resolver
        self._store = store
        self._tree_loader = tree_loader
        self._subtree_loader = subtree_loader
        self._provider = provider.strip()
        self._target_validator = target_validator
        self._revision_factory = revision_factory
        self._max_depth = max_depth
        self._max_snapshot_elements = max_snapshot_elements

    async def observe(
        self,
        request: ObserveTargetRequest,
        *,
        provider_identity: object,
        adapter_identity: object,
        budget: OperationBudget | None = None,
    ) -> TargetObservationResult:
        """Return one compact observation without provider retries or tree searches."""
        operation_budget = budget or OperationBudget.start(request.deadline_ms / 1_000)
        operation_budget.checkpoint("target observation")
        resolution = await self._resolver.resolve(
            request.target,
            provider_identity=provider_identity,
            adapter_identity=adapter_identity,
            activate=request.intent is TargetIntent.INTERACT,
            budget=operation_budget,
        )
        if resolution.target.browser_target is not None and not resolution.activated:
            validator = self._target_validator
            if validator is None or not await operation_budget.wait_for(
                validator(adapter_identity, resolution, operation_budget),
                operation="browser target identity validation",
            ):
                raise OperationError(
                    OperationErrorCode.FOCUS_MISMATCH,
                    "the requested browser target is not the visible native tab",
                )
        root, kind = await self._load_once(
            resolution,
            adapter_identity=adapter_identity,
            budget=operation_budget,
        )
        validator = self._target_validator
        if (
            resolution.target.browser_target is not None
            and validator is not None
            and not await operation_budget.wait_for(
                validator(adapter_identity, resolution, operation_budget),
                operation="post-scan browser target identity validation",
            )
        ):
            raise OperationError(
                OperationErrorCode.FOCUS_MISMATCH,
                "the visible native browser target changed during observation",
            )
        if root is None:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "the resolved target did not expose an accessibility tree",
            )

        index = LocatorIndex.from_roots(root)
        available_elements = index.elements
        snapshot_elements = available_elements[: self._max_snapshot_elements]
        pid = resolution.target.pid
        if pid is not None:
            for element in snapshot_elements:
                element.pid = pid

        snapshot = self._store.create(
            provider=self._provider,
            mode=OperationMode(resolution.target.mode.value),
            target_id=resolution.target.target_id,
            generation=0,
            revision=self._revision_factory(),
            elements=(
                ElementRecord(local_id=element.id, value=element)
                for element in snapshot_elements
            ),
            detach_ui_trees=True,
            truncated=len(snapshot_elements) < len(available_elements),
        )

        matched = index.find_many(request.selectors)
        stored_identities = {id(element) for element in snapshot_elements}
        selector_results = tuple(
            _selector_matches(
                selector_index,
                matches,
                stored_identities=stored_identities,
                max_results=request.max_results,
            )
            for selector_index, matches in enumerate(matched)
        )
        return TargetObservationResult(
            target=ObservedTarget(
                target_id=resolution.target.target_id,
                mode=resolution.target.mode.value,
                source=resolution.target.source.value,
                pid=resolution.target.pid,
                activated=resolution.activated,
            ),
            snapshot=snapshot,
            scan=ObservationScan(
                kind=kind,
                nodes=len(snapshot_elements),
                available_nodes=len(available_elements),
                truncated=len(snapshot_elements) < len(available_elements),
                inventory_cache=resolution.cache_status.value,
            ),
            selectors=selector_results,
            index=index,
        )

    async def _load_once(
        self,
        resolution: TargetResolution,
        *,
        adapter_identity: object,
        budget: OperationBudget,
    ) -> tuple[UIElement | None, ObservationLoadKind]:
        browser_target = resolution.target.browser_target
        if browser_target is not None:
            live_window = browser_target.window_element
            if live_window is None or live_window.platform_ref is None:
                raise OperationError(
                    OperationErrorCode.STALE_SNAPSHOT,
                    "the resolved browser window reference is unavailable",
                )
            root = await budget.wait_for(
                self._subtree_loader(
                    adapter_identity,
                    live_window,
                    self._max_depth,
                    budget,
                ),
                operation="target accessibility subtree",
            )
            return root, ObservationLoadKind.SUBTREE

        if resolution.target.pid is None:
            raise OperationError(
                OperationErrorCode.UNSUPPORTED_CAPABILITY,
                "the resolved target cannot provide a native accessibility tree",
            )
        root = await budget.wait_for(
            self._tree_loader(
                adapter_identity,
                resolution.target.pid,
                self._max_depth,
                budget,
            ),
            operation="target accessibility tree",
        )
        return root, ObservationLoadKind.TREE


def _selector_matches(
    index: int,
    matches: tuple[UIElement, ...],
    *,
    stored_identities: set[int],
    max_results: int,
) -> SelectorMatches:
    stored_matches = tuple(
        element for element in matches if id(element) in stored_identities
    )
    bounded = stored_matches[:max_results]
    total = len(matches)
    if total == 0:
        status = SelectorMatchStatus.MISSING
    elif total == 1:
        status = SelectorMatchStatus.UNIQUE
    else:
        status = SelectorMatchStatus.AMBIGUOUS
    return SelectorMatches(
        index=index,
        status=status,
        total=total,
        truncated=len(bounded) < total,
        matches=tuple(_compact_element(element) for element in bounded),
    )


def _compact_element(element: UIElement) -> CompactElement:
    secure = element._is_secure()
    return CompactElement(
        local_id=element.id,
        role=_compact_text(element.role, _MAX_ROLE_BYTES),
        name=_compact_text(element.name, _MAX_NAME_BYTES),
        value="" if secure else _compact_text(element.value, _MAX_VALUE_BYTES),
        states=tuple(
            _compact_text(state, _MAX_STATE_BYTES)
            for state in element.states[:_MAX_STATES]
            if state != "enabled"
        ),
        actions=tuple(
            _compact_text(action, _MAX_ACTION_BYTES)
            for action in element.actions[:_MAX_ACTIONS]
        ),
    )


def _compact_text(value: object, max_bytes: int) -> str:
    normalized = " ".join(str(value).split())
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return normalized
    suffix = "…".encode()
    prefix = encoded[: max(0, max_bytes - len(suffix))].decode(
        "utf-8",
        errors="ignore",
    )
    return prefix + "…"


def _encode(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
