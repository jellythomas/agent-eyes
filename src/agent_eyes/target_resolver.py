"""Provider-neutral exact target resolution with a short inventory cache."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TypeAlias

from .browser_inventory import BrowserTarget, rank_browser_targets
from .operation import OperationBudget, OperationError, OperationErrorCode
from .transaction_contract import TargetMode, TargetSpec


INVENTORY_CACHE_TTL_SECONDS = 0.3
EXACT_TARGET_LEASE_TTL_SECONDS = 60.0
DEFAULT_MINIMUM_BROWSER_SCORE = 60


class ResolutionSource(Enum):
    """How a target was selected without exposing caller input."""

    EXACT = "exact"
    PID = "pid"
    QUERY = "query"


class InventoryCacheStatus(Enum):
    """Per-resolution inventory behavior suitable for redacted telemetry."""

    BYPASS = "bypass"
    MISS = "miss"
    SHARED = "shared"
    HIT = "hit"


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    """An opaque exact target supplied by a foreground or shadow provider."""

    target_id: str
    pid: int | None = None
    value: object | None = field(default=None, repr=False, compare=False)


InventoryTarget: TypeAlias = BrowserTarget | ProviderTarget


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """One exact provider target selected for a bounded operation."""

    mode: TargetMode
    target_id: str
    pid: int | None
    source: ResolutionSource
    browser_target: BrowserTarget | None = None
    provider_target: ProviderTarget | None = None


@dataclass(frozen=True, slots=True)
class TargetResolution:
    """Resolved target plus cache and activation metadata."""

    target: ResolvedTarget
    cache_status: InventoryCacheStatus
    activated: bool


InventoryProducer: TypeAlias = Callable[
    [object, object, TargetMode], Awaitable[Iterable[InventoryTarget]]
]
ActivationCallback: TypeAlias = Callable[
    [object, object, ResolvedTarget], Awaitable[bool]
]


@dataclass(frozen=True, slots=True, eq=False)
class _InventoryKey:
    provider_identity: object
    adapter_identity: object
    mode: TargetMode

    def __hash__(self) -> int:
        return hash((id(self.provider_identity), id(self.adapter_identity), self.mode))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _InventoryKey)
            and self.provider_identity is other.provider_identity
            and self.adapter_identity is other.adapter_identity
            and self.mode is other.mode
        )


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    targets: tuple[InventoryTarget, ...]
    expires_at: float


@dataclass(slots=True)
class _InventoryFlight:
    task: asyncio.Task[tuple[InventoryTarget, ...]]
    waiters: int = 0


class TargetResolver:
    """Resolve exact desktop, browser, and shadow targets without server coupling."""

    def __init__(
        self,
        inventory_producer: InventoryProducer,
        activation_callback: ActivationCallback,
        *,
        clock: Callable[[], float] = time.monotonic,
        minimum_browser_score: int = DEFAULT_MINIMUM_BROWSER_SCORE,
    ) -> None:
        if (
            isinstance(minimum_browser_score, bool)
            or not isinstance(minimum_browser_score, int)
            or minimum_browser_score < 0
        ):
            raise ValueError("minimum_browser_score must be a non-negative integer")
        self._inventory_producer = inventory_producer
        self._activation_callback = activation_callback
        self._clock = clock
        self._minimum_browser_score = minimum_browser_score
        self._cache: dict[_InventoryKey, _CacheEntry] = {}
        self._exact_leases: dict[_InventoryKey, dict[str, _CacheEntry]] = {}
        self._flights: dict[_InventoryKey, _InventoryFlight] = {}

    async def resolve(
        self,
        spec: TargetSpec,
        *,
        provider_identity: object,
        adapter_identity: object,
        activate: bool = False,
        budget: OperationBudget | None = None,
    ) -> TargetResolution:
        """Resolve one target and optionally perform exact foreground activation."""
        if provider_identity is None or adapter_identity is None:
            raise ValueError("provider and adapter identities must not be None")

        source = self._validate_spec(spec)
        cache_status = InventoryCacheStatus.BYPASS
        if source is ResolutionSource.PID:
            assert spec.pid is not None
            resolved = ResolvedTarget(
                mode=spec.mode,
                target_id=f"pid:{spec.pid}",
                pid=spec.pid,
                source=source,
            )
        elif source is ResolutionSource.EXACT and spec.mode is TargetMode.FOREGROUND:
            leased = self._leased_exact_target(
                provider_identity,
                adapter_identity,
                spec.mode,
                spec.target_id,
            )
            if leased is None:
                raise OperationError(
                    OperationErrorCode.ELEMENT_NOT_FOUND,
                    "exact native target is stale; refresh browser inventory",
                )
            targets = (leased,)
            cache_status = InventoryCacheStatus.HIT
            resolved = self._resolve_exact(spec, targets)
        else:
            targets, cache_status = await self._inventory(
                provider_identity,
                adapter_identity,
                spec.mode,
                budget=budget,
            )
            if source is ResolutionSource.QUERY:
                resolved = self._resolve_query(spec, targets)
                if resolved.browser_target is not None:
                    self._remember_exact_browser_target(
                        resolved.browser_target,
                        provider_identity=provider_identity,
                        adapter_identity=adapter_identity,
                        mode=resolved.mode,
                    )
            else:
                resolved = self._resolve_exact(spec, targets)

        activated = await self._activate(
            resolved,
            requested=activate,
            provider_identity=provider_identity,
            adapter_identity=adapter_identity,
            budget=budget,
        )
        return TargetResolution(
            target=resolved,
            cache_status=cache_status,
            activated=activated,
        )

    def invalidate(
        self,
        *,
        provider_identity: object | None = None,
        adapter_identity: object | None = None,
        mode: TargetMode | None = None,
    ) -> None:
        """Invalidate matching completed and in-flight inventory generations."""
        matching_cache = [
            key
            for key in self._cache
            if self._matches_invalidation(
                key,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=mode,
            )
        ]
        for key in matching_cache:
            self._cache.pop(key, None)

        matching_leases = [
            key
            for key in self._exact_leases
            if self._matches_invalidation(
                key,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=mode,
            )
        ]
        for key in matching_leases:
            self._exact_leases.pop(key, None)

        matching_flights = [
            key
            for key in self._flights
            if self._matches_invalidation(
                key,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=mode,
            )
        ]
        for key in matching_flights:
            self._flights.pop(key, None)

    def cached_targets(
        self,
        *,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
    ) -> tuple[InventoryTarget, ...]:
        """Return only a still-live completed inventory without starting a scan."""
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        entry = self._cache.get(
            _InventoryKey(provider_identity, adapter_identity, mode)
        )
        return () if entry is None else entry.targets

    def remember_targets(
        self,
        *,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
        targets: Iterable[InventoryTarget],
    ) -> None:
        """Retain opaque exact native IDs with their original live provider refs."""
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        key = _InventoryKey(provider_identity, adapter_identity, mode)
        bucket = {
            target.identifier: _CacheEntry(
                targets=(target,),
                expires_at=now + EXACT_TARGET_LEASE_TTL_SECONDS,
            )
            for target in targets
            if isinstance(target, BrowserTarget)
        }
        if bucket:
            self._exact_leases[key] = bucket
        else:
            self._exact_leases.pop(key, None)

    def prime_inventory(
        self,
        *,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
        targets: Iterable[InventoryTarget],
    ) -> None:
        """Install one already-completed strict inventory without another scan."""
        observed = tuple(targets)
        if any(
            not isinstance(target, (BrowserTarget, ProviderTarget))
            for target in observed
        ):
            raise TypeError("inventory contains an unsupported target type")
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        key = _InventoryKey(provider_identity, adapter_identity, mode)
        self._cache[key] = _CacheEntry(
            targets=observed,
            expires_at=now + INVENTORY_CACHE_TTL_SECONDS,
        )
        self.remember_targets(
            provider_identity=provider_identity,
            adapter_identity=adapter_identity,
            mode=mode,
            targets=observed,
        )

    def _validate_spec(self, spec: TargetSpec) -> ResolutionSource:
        target_id = spec.target_id.strip()
        query = spec.query.strip()
        has_pid = spec.pid is not None
        pid_valid = (
            has_pid
            and not isinstance(spec.pid, bool)
            and isinstance(spec.pid, int)
            and spec.pid > 0
        )
        selector_count = int(bool(target_id)) + int(bool(query)) + int(has_pid)
        if selector_count != 1:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "target requires exactly one exact identifier, PID, or query",
            )
        if has_pid and not pid_valid:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "target PID must be a positive integer",
            )

        if spec.mode is TargetMode.SHADOW:
            if not target_id or target_id.startswith("native:") or pid_valid or query:
                raise OperationError(
                    OperationErrorCode.MODE_MISMATCH,
                    "shadow resolution requires one exact shadow target identifier",
                )
            return ResolutionSource.EXACT

        if spec.mode is not TargetMode.FOREGROUND:
            raise OperationError(
                OperationErrorCode.MODE_MISMATCH,
                "target mode is not supported",
            )
        if target_id:
            if not target_id.startswith("native:"):
                raise OperationError(
                    OperationErrorCode.MODE_MISMATCH,
                    "foreground exact resolution requires a native target identifier",
                )
            return ResolutionSource.EXACT
        if pid_valid:
            return ResolutionSource.PID
        return ResolutionSource.QUERY

    async def _inventory(
        self,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
        *,
        budget: OperationBudget | None,
    ) -> tuple[tuple[InventoryTarget, ...], InventoryCacheStatus]:
        key = _InventoryKey(provider_identity, adapter_identity, mode)
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        entry = self._cache.get(key)
        if entry is not None and now < entry.expires_at:
            return entry.targets, InventoryCacheStatus.HIT

        flight = self._flights.get(key)
        if flight is None:
            task = asyncio.create_task(
                self._produce_inventory(key),
                name="agent-eyes-target-inventory",
            )
            flight = _InventoryFlight(task=task)
            self._flights[key] = flight
            task.add_done_callback(
                lambda done, flight_key=key: self._inventory_finished(flight_key, done)
            )
            status = InventoryCacheStatus.MISS
        else:
            status = InventoryCacheStatus.SHARED
        flight.waiters += 1

        try:
            shared = asyncio.shield(flight.task)
            targets = (
                await budget.wait_for(shared, operation="target inventory")
                if budget is not None
                else await shared
            )
            return targets, status
        finally:
            self._release_waiter(key, flight)

    async def _produce_inventory(
        self,
        key: _InventoryKey,
    ) -> tuple[InventoryTarget, ...]:
        produced = await self._inventory_producer(
            key.provider_identity,
            key.adapter_identity,
            key.mode,
        )
        targets = tuple(produced)
        if any(
            not isinstance(target, (BrowserTarget, ProviderTarget))
            for target in targets
        ):
            raise TypeError("inventory producer returned an unsupported target type")

        current_task = asyncio.current_task()
        flight = self._flights.get(key)
        if flight is not None and flight.task is current_task:
            completed_at = self._clock()
            if not math.isfinite(completed_at):
                raise ValueError("clock must return a finite monotonic value")
            self._cache[key] = _CacheEntry(
                targets=targets,
                expires_at=completed_at + INVENTORY_CACHE_TTL_SECONDS,
            )
        return targets

    def _inventory_finished(
        self,
        key: _InventoryKey,
        task: asyncio.Task[tuple[InventoryTarget, ...]],
    ) -> None:
        if not task.cancelled():
            task.exception()
        flight = self._flights.get(key)
        if flight is not None and flight.task is task:
            self._flights.pop(key, None)

    def _release_waiter(
        self,
        key: _InventoryKey,
        flight: _InventoryFlight,
    ) -> None:
        flight.waiters -= 1
        if flight.waiters == 0:
            if not flight.task.done():
                flight.task.cancel()
            if self._flights.get(key) is flight:
                self._flights.pop(key, None)

    def _resolve_exact(
        self,
        spec: TargetSpec,
        targets: tuple[InventoryTarget, ...],
    ) -> ResolvedTarget:
        matches = [
            target for target in targets if self._target_id(target) == spec.target_id
        ]
        if not matches:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "exact provider target is not present",
            )
        if len(matches) > 1:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_TARGET,
                "exact provider target is not unique",
            )

        match = matches[0]
        if isinstance(match, BrowserTarget):
            return ResolvedTarget(
                mode=spec.mode,
                target_id=match.identifier,
                pid=match.pid,
                source=ResolutionSource.EXACT,
                browser_target=match,
            )
        return ResolvedTarget(
            mode=spec.mode,
            target_id=match.target_id,
            pid=match.pid,
            source=ResolutionSource.EXACT,
            provider_target=match,
        )

    def _resolve_query(
        self,
        spec: TargetSpec,
        targets: tuple[InventoryTarget, ...],
    ) -> ResolvedTarget:
        ranked = rank_browser_targets(
            (target for target in targets if isinstance(target, BrowserTarget)),
            spec.query,
        )
        if not ranked or ranked[0].score < self._minimum_browser_score:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                "no browser target met the query threshold",
            )
        if len(ranked) > 1 and ranked[1].score == ranked[0].score:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_TARGET,
                "multiple browser targets share the top query score",
            )

        match = ranked[0]
        return ResolvedTarget(
            mode=TargetMode.FOREGROUND,
            target_id=match.identifier,
            pid=match.pid,
            source=ResolutionSource.QUERY,
            browser_target=match,
        )

    async def _activate(
        self,
        target: ResolvedTarget,
        *,
        requested: bool,
        provider_identity: object,
        adapter_identity: object,
        budget: OperationBudget | None,
    ) -> bool:
        if not requested or target.mode is TargetMode.SHADOW:
            return False

        try:
            pending = self._activation_callback(
                provider_identity,
                adapter_identity,
                target,
            )
            activated = (
                await budget.wait_for(pending, operation="target activation")
                if budget is not None
                else await pending
            )
        except BaseException:
            self.invalidate(
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=target.mode,
            )
            raise
        if not activated:
            self.invalidate(
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=target.mode,
            )
            raise OperationError(
                OperationErrorCode.FOCUS_MISMATCH,
                "the exact foreground target could not be activated",
            )
        if target.browser_target is None:
            self.invalidate(
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=target.mode,
            )
        else:
            self._record_browser_activation(
                target,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
            )
        return True

    def _record_browser_activation(
        self,
        target: ResolvedTarget,
        *,
        provider_identity: object,
        adapter_identity: object,
    ) -> None:
        """Keep a short exact-target lease while updating cached focus ranking."""
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        for key, entry in tuple(self._cache.items()):
            if not self._matches_invalidation(
                key,
                provider_identity=provider_identity,
                adapter_identity=adapter_identity,
                mode=target.mode,
            ):
                continue
            updated: list[InventoryTarget] = []
            for candidate in entry.targets:
                if not isinstance(candidate, BrowserTarget):
                    updated.append(candidate)
                    continue
                selected = candidate.identifier == target.target_id
                updated.append(
                    replace(
                        candidate,
                        selected=selected,
                        frontmost=candidate.pid == target.pid,
                    )
                )
            self._cache[key] = _CacheEntry(
                targets=tuple(updated),
                expires_at=entry.expires_at,
            )

        browser_target = target.browser_target
        assert browser_target is not None
        self._remember_exact_browser_target(
            replace(browser_target, selected=True, frontmost=True),
            provider_identity=provider_identity,
            adapter_identity=adapter_identity,
            mode=target.mode,
        )

    def _remember_exact_browser_target(
        self,
        browser_target: BrowserTarget,
        *,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
    ) -> None:
        """Bind one opaque target ID to its original provider references."""
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        key = _InventoryKey(
            provider_identity,
            adapter_identity,
            mode,
        )
        self._exact_leases.setdefault(key, {})[browser_target.identifier] = _CacheEntry(
            targets=(browser_target,),
            expires_at=now + EXACT_TARGET_LEASE_TTL_SECONDS,
        )

    def _leased_exact_target(
        self,
        provider_identity: object,
        adapter_identity: object,
        mode: TargetMode,
        target_id: str,
    ) -> InventoryTarget | None:
        """Return one activation-verified target lease without refreshing queries."""
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic value")
        self._prune_expired(now)
        bucket = self._exact_leases.get(
            _InventoryKey(provider_identity, adapter_identity, mode)
        )
        if bucket is None:
            return None
        entry = bucket.get(target_id)
        if entry is None or not entry.targets:
            return None
        return entry.targets[0]

    def _prune_expired(self, now: float) -> None:
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)
        for key, bucket in tuple(self._exact_leases.items()):
            for target_id, entry in tuple(bucket.items()):
                if entry.expires_at <= now:
                    bucket.pop(target_id, None)
            if not bucket:
                self._exact_leases.pop(key, None)

    @staticmethod
    def _target_id(target: InventoryTarget) -> str:
        if isinstance(target, BrowserTarget):
            return target.identifier
        return target.target_id

    @staticmethod
    def _matches_invalidation(
        key: _InventoryKey,
        *,
        provider_identity: object | None,
        adapter_identity: object | None,
        mode: TargetMode | None,
    ) -> bool:
        return (
            (provider_identity is None or key.provider_identity is provider_identity)
            and (adapter_identity is None or key.adapter_identity is adapter_identity)
            and (mode is None or key.mode is mode)
        )
