"""Immutable, provider-qualified observation snapshots."""
from __future__ import annotations

import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from .adapters.base import UIElement
from .operation import OperationError, OperationErrorCode, OperationMode


@dataclass(frozen=True, slots=True)
class ElementRecord:
    """A short local ID and its provider-owned reference."""

    local_id: int
    value: Any
    actionable: bool = True


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    """An immutable observation bound to one exact provider target state."""

    token: str
    provider: str
    mode: OperationMode
    target_id: str
    generation: int
    revision: int
    created_at: float
    elements: tuple[ElementRecord, ...]
    truncated: bool = False


@dataclass(slots=True)
class _StoredSnapshot:
    snapshot: ObservationSnapshot
    index: dict[int, ElementRecord]
    release: Callable[[object], None] | None


class ObservationStore:
    """A bounded store for immutable observation snapshots."""

    def __init__(
        self,
        *,
        max_snapshots: int = 32,
        max_elements_per_snapshot: int = 500,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            isinstance(max_snapshots, bool)
            or not isinstance(max_snapshots, int)
            or max_snapshots < 1
        ):
            raise ValueError("max_snapshots must be a positive integer")
        if (
            isinstance(max_elements_per_snapshot, bool)
            or not isinstance(max_elements_per_snapshot, int)
            or max_elements_per_snapshot < 1
        ):
            raise ValueError("max_elements_per_snapshot must be a positive integer")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive")
        self._max_snapshots = max_snapshots
        self._max_elements_per_snapshot = max_elements_per_snapshot
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory or self._new_token
        self._snapshots: OrderedDict[str, _StoredSnapshot] = OrderedDict()
        self._closed = False

    @staticmethod
    def _new_token() -> str:
        return f"n{secrets.token_urlsafe(6)}"

    def create(
        self,
        *,
        provider: str,
        mode: OperationMode,
        target_id: str,
        generation: int,
        revision: int,
        elements: Iterable[ElementRecord],
        release: Callable[[object], None] | None = None,
        detach_ui_trees: bool = False,
        truncated: bool = False,
    ) -> ObservationSnapshot:
        self._ensure_open()
        if not provider:
            raise ValueError("provider is required")
        if not isinstance(mode, OperationMode):
            raise ValueError("mode must be an OperationMode")
        if not target_id:
            raise ValueError("target_id is required")
        if generation < 0 or revision < 0:
            raise ValueError("generation and revision must be non-negative")
        if not isinstance(detach_ui_trees, bool):
            raise ValueError("detach_ui_trees must be a boolean")
        if not isinstance(truncated, bool):
            raise ValueError("truncated must be a boolean")

        records: list[ElementRecord] = []
        index: dict[int, ElementRecord] = {}
        stored = False
        try:
            for record in elements:
                records.append(record)
                if len(records) > self._max_elements_per_snapshot:
                    raise ValueError(
                        "snapshot may contain at most "
                        f"{self._max_elements_per_snapshot} elements"
                    )
                if (
                    isinstance(record.local_id, bool)
                    or not isinstance(record.local_id, int)
                    or record.local_id < 0
                ):
                    raise ValueError(
                        "element local IDs must be non-negative integers"
                    )
                if record.local_id in index:
                    raise ValueError(
                        f"duplicate local element ID: {record.local_id}"
                    )
                index[record.local_id] = record

            if detach_ui_trees:
                records = self._detach_ui_trees(records)
                index = {record.local_id: record for record in records}

            self.evict_expired()
            token = self._unique_token()
            snapshot = ObservationSnapshot(
                token=token,
                provider=provider,
                mode=mode,
                target_id=target_id,
                generation=generation,
                revision=revision,
                created_at=self._clock(),
                elements=tuple(records),
                truncated=truncated,
            )
            self._snapshots[token] = _StoredSnapshot(snapshot, index, release)
            stored = True
        finally:
            if not stored:
                self._release_records(records, release)

        while len(self._snapshots) > self._max_snapshots:
            oldest_token = next(iter(self._snapshots))
            self._evict(oldest_token)
        return snapshot

    def resolve(
        self,
        token: str,
        local_id: int,
        *,
        expected_provider: str | None = None,
        expected_mode: OperationMode | None = None,
        expected_target_id: str | None = None,
        expected_generation: int | None = None,
    ) -> ElementRecord:
        self.get_snapshot(
            token,
            expected_provider=expected_provider,
            expected_mode=expected_mode,
            expected_target_id=expected_target_id,
            expected_generation=expected_generation,
        )
        stored = self._snapshots[token]
        record = stored.index.get(local_id)
        if record is None:
            raise OperationError(
                OperationErrorCode.ELEMENT_NOT_FOUND,
                f"element [{local_id}] is not present in snapshot {token}",
            )
        self._snapshots.move_to_end(token)
        return record

    def get_snapshot(
        self,
        token: str,
        *,
        expected_provider: str | None = None,
        expected_mode: OperationMode | None = None,
        expected_target_id: str | None = None,
        expected_generation: int | None = None,
    ) -> ObservationSnapshot:
        """Return live immutable metadata after applying target constraints."""
        self.evict_expired()
        stored = self._snapshots.get(token)
        if stored is None:
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "snapshot is missing, expired, or invalidated",
            )
        snapshot = stored.snapshot
        if expected_provider is not None and snapshot.provider != expected_provider:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "snapshot belongs to a different provider",
            )
        if expected_mode is not None and snapshot.mode is not expected_mode:
            raise OperationError(
                OperationErrorCode.MODE_MISMATCH,
                "snapshot belongs to a different execution mode",
            )
        if expected_target_id is not None and snapshot.target_id != expected_target_id:
            raise OperationError(
                OperationErrorCode.TARGET_MISMATCH,
                "snapshot belongs to a different target",
            )
        if expected_generation is not None and snapshot.generation != expected_generation:
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "snapshot belongs to an older target generation",
            )
        self._snapshots.move_to_end(token)
        return snapshot

    def resolve_with_snapshot(
        self,
        token: str,
        local_id: int,
        *,
        expected_provider: str | None = None,
        expected_mode: OperationMode | None = None,
        expected_target_id: str | None = None,
        expected_generation: int | None = None,
    ) -> tuple[ObservationSnapshot, ElementRecord]:
        """Resolve an element and retain its immutable target metadata."""
        record = self.resolve(
            token,
            local_id,
            expected_provider=expected_provider,
            expected_mode=expected_mode,
            expected_target_id=expected_target_id,
            expected_generation=expected_generation,
        )
        stored = self._snapshots.get(token)
        if stored is None:  # Defensive: resolve() already proved it is live.
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                "snapshot was invalidated during resolution",
            )
        return stored.snapshot, record

    def resolve_legacy(
        self,
        local_id: int,
        *,
        expected_mode: OperationMode,
    ) -> tuple[ObservationSnapshot, ElementRecord]:
        self.evict_expired()
        matches = [
            (stored.snapshot, record)
            for stored in self._snapshots.values()
            if stored.snapshot.mode is expected_mode
            if (record := stored.index.get(local_id)) is not None
        ]
        if not matches:
            raise OperationError(
                OperationErrorCode.STALE_SNAPSHOT,
                f"element [{local_id}] has no live originating snapshot",
            )
        if len(matches) != 1:
            raise OperationError(
                OperationErrorCode.AMBIGUOUS_TARGET,
                f"element [{local_id}] appears in multiple live snapshots; provide snapshot",
            )
        return matches[0]

    def invalidate_target(
        self,
        *,
        provider: str,
        mode: OperationMode,
        target_id: str,
    ) -> int:
        tokens = [
            token
            for token, stored in self._snapshots.items()
            if stored.snapshot.provider == provider
            and stored.snapshot.mode is mode
            and stored.snapshot.target_id == target_id
        ]
        for token in tokens:
            self._evict(token)
        return len(tokens)

    def invalidate_provider(
        self,
        *,
        provider: str,
        mode: OperationMode,
    ) -> int:
        """Invalidate every snapshot for one provider in one execution mode."""
        tokens = [
            token
            for token, stored in self._snapshots.items()
            if stored.snapshot.provider == provider
            and stored.snapshot.mode is mode
        ]
        for token in tokens:
            self._evict(token)
        return len(tokens)

    def evict_expired(self) -> int:
        now = self._clock()
        tokens = [
            token
            for token, stored in self._snapshots.items()
            if now - stored.snapshot.created_at >= self._ttl_seconds
        ]
        for token in tokens:
            self._evict(token)
        return len(tokens)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for token in list(self._snapshots):
            self._evict(token)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("observation store is closed")

    def _unique_token(self) -> str:
        for _ in range(16):
            token = self._token_factory()
            if token and token not in self._snapshots:
                return token
        raise RuntimeError("could not allocate a unique snapshot token")

    def _evict(self, token: str) -> None:
        stored = self._snapshots.pop(token, None)
        if stored is None or stored.release is None:
            return
        self._release_records(stored.snapshot.elements, stored.release)

    @staticmethod
    def _detach_ui_trees(records: list[ElementRecord]) -> list[ElementRecord]:
        """Clone only stored UI nodes and edges so the record cap bounds memory."""
        originals = tuple(
            record.value
            for record in records
            if isinstance(record.value, UIElement)
        )
        clones = {
            id(element): replace(
                element,
                states=list(element.states),
                actions=list(element.actions),
                children=[],
            )
            for element in originals
        }
        for element in originals:
            clone = clones[id(element)]
            clone.children = [
                clones[id(child)]
                for child in element.children
                if id(child) in clones
            ]
        return [
            replace(record, value=clones[id(record.value)])
            if isinstance(record.value, UIElement)
            else record
            for record in records
        ]

    @staticmethod
    def _release_records(
        records: Iterable[ElementRecord],
        release: Callable[[object], None] | None,
    ) -> None:
        if release is None:
            return
        for record in records:
            try:
                release(record.value)
            except Exception:
                continue
