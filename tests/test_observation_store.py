from __future__ import annotations

import gc
import weakref
from dataclasses import FrozenInstanceError

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationError, OperationErrorCode, OperationMode


class FakeClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _record(local_id: int, value: object) -> ElementRecord:
    return ElementRecord(local_id=local_id, value=value)


def test_same_local_id_resolves_only_inside_its_snapshot():
    store = ObservationStore()
    first = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:101/window:a",
        generation=1,
        revision=1,
        elements=[_record(2, "first button")],
    )
    second = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:202/window:b",
        generation=1,
        revision=1,
        elements=[_record(2, "second button")],
    )

    assert store.resolve(first.token, 2).value == "first button"
    assert store.resolve(second.token, 2).value == "second button"


def test_qualified_resolution_returns_immutable_snapshot_metadata():
    store = ObservationStore()
    snapshot = store.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-7",
        generation=3,
        revision=11,
        elements=[_record(9, "button")],
    )

    resolved_snapshot, record = store.resolve_with_snapshot(
        snapshot.token,
        9,
        expected_provider="cdp-persistent",
        expected_mode=OperationMode.SHADOW,
        expected_target_id="target-7",
        expected_generation=3,
    )

    assert resolved_snapshot is snapshot
    assert record.value == "button"


def test_get_snapshot_returns_live_metadata_without_element_lookup():
    store = ObservationStore()
    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:88",
        generation=0,
        revision=4,
        elements=[_record(1, "one"), _record(2, "two")],
    )

    assert store.get_snapshot(
        snapshot.token,
        expected_mode=OperationMode.FOREGROUND,
    ) is snapshot


def test_legacy_id_resolution_fails_when_more_than_one_snapshot_matches():
    store = ObservationStore()
    for target_id in ("target-a", "target-b"):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=target_id,
            generation=1,
            revision=1,
            elements=[_record(1, target_id)],
        )

    with pytest.raises(OperationError) as exc_info:
        store.resolve_legacy(1, expected_mode=OperationMode.FOREGROUND)

    assert exc_info.value.code is OperationErrorCode.AMBIGUOUS_TARGET


def test_legacy_id_resolution_succeeds_only_for_one_live_snapshot():
    store = ObservationStore()
    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target-a",
        generation=3,
        revision=8,
        elements=[_record(7, "only")],
    )

    resolved_snapshot, record = store.resolve_legacy(
        7, expected_mode=OperationMode.FOREGROUND
    )

    assert resolved_snapshot is snapshot
    assert record.value == "only"


def test_mode_mismatch_fails_closed():
    store = ObservationStore()
    snapshot = store.create(
        provider="cdp",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=1,
        revision=1,
        elements=[_record(4, "remote")],
    )

    with pytest.raises(OperationError) as exc_info:
        store.resolve(
            snapshot.token,
            4,
            expected_mode=OperationMode.FOREGROUND,
        )

    assert exc_info.value.code is OperationErrorCode.MODE_MISMATCH


def test_expected_target_and_generation_are_verified():
    store = ObservationStore()
    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:22/window:3",
        generation=5,
        revision=9,
        elements=[_record(1, "button")],
    )

    with pytest.raises(OperationError) as wrong_target:
        store.resolve(snapshot.token, 1, expected_target_id="pid:22/window:4")
    with pytest.raises(OperationError) as stale_generation:
        store.resolve(snapshot.token, 1, expected_generation=6)

    assert wrong_target.value.code is OperationErrorCode.TARGET_MISMATCH
    assert stale_generation.value.code is OperationErrorCode.STALE_SNAPSHOT


def test_target_invalidation_removes_only_matching_snapshots():
    store = ObservationStore()
    stale = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target-a",
        generation=1,
        revision=1,
        elements=[_record(1, "a")],
    )
    live = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target-b",
        generation=1,
        revision=1,
        elements=[_record(1, "b")],
    )

    assert store.invalidate_target(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target-a",
    ) == 1

    with pytest.raises(OperationError) as exc_info:
        store.resolve(stale.token, 1)
    assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT
    assert store.resolve(live.token, 1).value == "b"


def test_provider_invalidation_is_scoped_by_provider_and_mode():
    store = ObservationStore()
    stale = [
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id=target_id,
            generation=1,
            revision=1,
            elements=[_record(1, target_id)],
        )
        for target_id in ("target-a", "target-b")
    ]
    live_shadow = store.create(
        provider="native",
        mode=OperationMode.SHADOW,
        target_id="target-a",
        generation=1,
        revision=1,
        elements=[_record(1, "shadow")],
    )
    live_provider = store.create(
        provider="cdp-persistent",
        mode=OperationMode.FOREGROUND,
        target_id="target-a",
        generation=1,
        revision=1,
        elements=[_record(1, "cdp")],
    )

    assert store.invalidate_provider(
        provider="native",
        mode=OperationMode.FOREGROUND,
    ) == 2

    for snapshot in stale:
        with pytest.raises(OperationError) as exc_info:
            store.resolve(snapshot.token, 1)
        assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT
    assert store.resolve(live_shadow.token, 1).value == "shadow"
    assert store.resolve(live_provider.token, 1).value == "cdp"


def test_ttl_expiry_calls_release_hook_once():
    clock = FakeClock()
    released: list[object] = []
    store = ObservationStore(ttl_seconds=2.0, clock=clock)
    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target-a",
        generation=1,
        revision=1,
        elements=[_record(1, "native-ref")],
        release=released.append,
    )
    clock.advance(2.0)

    with pytest.raises(OperationError):
        store.resolve(snapshot.token, 1)
    store.evict_expired()

    assert released == ["native-ref"]


def test_capacity_evicts_oldest_snapshot_and_releases_it():
    released: list[object] = []
    store = ObservationStore(max_snapshots=1)
    oldest = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="oldest",
        generation=1,
        revision=1,
        elements=[_record(1, "old-ref")],
        release=released.append,
    )
    newest = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="newest",
        generation=1,
        revision=1,
        elements=[_record(1, "new-ref")],
        release=released.append,
    )

    with pytest.raises(OperationError):
        store.resolve(oldest.token, 1)
    assert store.resolve(newest.token, 1).value == "new-ref"
    assert released == ["old-ref"]


def test_per_snapshot_element_bound_stops_consuming_and_releases_owned_records():
    consumed: list[int] = []
    released: list[object] = []
    store = ObservationStore(max_elements_per_snapshot=2)

    def records():
        for local_id in range(10):
            consumed.append(local_id)
            yield _record(local_id, f"ref-{local_id}")

    with pytest.raises(ValueError, match="at most 2 elements"):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="bounded",
            generation=1,
            revision=1,
            elements=records(),
            release=released.append,
        )

    assert consumed == [0, 1, 2]
    assert released == ["ref-0", "ref-1", "ref-2"]
    assert store._snapshots == {}


def test_per_snapshot_element_bound_accepts_the_exact_limit():
    store = ObservationStore(max_elements_per_snapshot=2)

    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="bounded",
        generation=1,
        revision=1,
        elements=[_record(1, "first"), _record(2, "second")],
    )

    assert [record.value for record in snapshot.elements] == ["first", "second"]


@pytest.mark.parametrize("eviction_path", ["capacity", "ttl", "invalidate", "close"])
def test_release_is_robust_and_exactly_once_for_every_eviction_path(eviction_path):
    clock = FakeClock()
    calls: dict[str, int] = {}

    def release(value: object) -> None:
        key = str(value)
        calls[key] = calls.get(key, 0) + 1
        if key == "raises":
            raise RuntimeError("provider cleanup failed")

    store = ObservationStore(
        max_snapshots=1 if eviction_path == "capacity" else 2,
        ttl_seconds=1.0,
        clock=clock,
    )
    store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="owned",
        generation=1,
        revision=1,
        elements=[_record(1, "raises"), _record(2, "continues")],
        release=release,
    )

    if eviction_path == "capacity":
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="replacement",
            generation=1,
            revision=1,
            elements=[],
        )
        assert store.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="owned",
        ) == 0
    elif eviction_path == "ttl":
        clock.advance(1.0)
        assert store.evict_expired() == 1
        assert store.evict_expired() == 0
    elif eviction_path == "invalidate":
        assert store.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="owned",
        ) == 1
        assert store.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="owned",
        ) == 0
    else:
        store.close()
        store.close()

    store.close()
    assert calls == {"raises": 1, "continues": 1}


def test_ten_thousand_create_invalidate_cycles_retain_no_provider_references():
    class ProviderReference:
        def __init__(self) -> None:
            self.payload = bytearray(4 * 1024)
            self.payload[0] = 1

    survivors: weakref.WeakSet[ProviderReference] = weakref.WeakSet()
    released = 0

    def release(_value: object) -> None:
        nonlocal released
        released += 1

    store = ObservationStore(max_snapshots=4)
    maximum_live_snapshots = 0
    for revision in range(10_000):
        provider_reference = ProviderReference()
        survivors.add(provider_reference)
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="reused-target",
            generation=1,
            revision=revision,
            elements=[_record(1, provider_reference)],
            release=release,
        )
        maximum_live_snapshots = max(maximum_live_snapshots, len(store._snapshots))
        del provider_reference
        assert store.invalidate_target(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="reused-target",
        ) == 1

    gc.collect()

    assert released == 10_000
    assert len(survivors) == 0
    assert maximum_live_snapshots == 1
    assert store._snapshots == {}


def test_snapshots_and_records_are_immutable():
    record = _record(1, "value")
    store = ObservationStore()
    snapshot = store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target",
        generation=1,
        revision=1,
        elements=[record],
    )

    with pytest.raises(FrozenInstanceError):
        record.local_id = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 2  # type: ignore[misc]


def test_detached_ui_tree_records_bound_recursive_retention_to_record_cap():
    store = ObservationStore(max_elements_per_snapshot=500)

    def create_deep_snapshot():
        nodes = [
            UIElement(id=index, role="group", name=f"node-{index}")
            for index in range(1, 1_001)
        ]
        for parent, child in zip(nodes, nodes[1:]):
            parent.children = [child]
        original_root = weakref.ref(nodes[0])
        excluded_tail = weakref.ref(nodes[-1])
        snapshot = store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="pid:73",
            generation=0,
            revision=1,
            elements=(
                ElementRecord(local_id=node.id, value=node)
                for node in nodes[:500]
            ),
            detach_ui_trees=True,
            truncated=True,
        )
        return snapshot, original_root, excluded_tail

    snapshot, original_root, excluded_tail = create_deep_snapshot()
    gc.collect()

    stored_root = snapshot.elements[0].value
    retained = 0
    current = stored_root
    while current is not None:
        retained += 1
        current = current.children[0] if current.children else None

    assert retained == 500
    assert snapshot.truncated is True
    assert original_root() is None
    assert excluded_tail() is None


def test_duplicate_local_ids_are_rejected():
    store = ObservationStore()

    with pytest.raises(ValueError, match="duplicate"):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="target",
            generation=1,
            revision=1,
            elements=[_record(1, "a"), _record(1, "b")],
        )


def test_close_releases_all_live_snapshots_and_rejects_new_ones():
    released: list[object] = []
    store = ObservationStore()
    store.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="target",
        generation=1,
        revision=1,
        elements=[_record(1, "ref")],
        release=released.append,
    )

    store.close()

    assert released == ["ref"]
    with pytest.raises(RuntimeError, match="closed"):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="other",
            generation=1,
            revision=1,
            elements=[],
        )


@pytest.mark.parametrize("max_snapshots", [0, -1, 1.5, True])
def test_invalid_capacity_is_rejected(max_snapshots):
    with pytest.raises(ValueError):
        ObservationStore(max_snapshots=max_snapshots)


@pytest.mark.parametrize("max_elements", [0, -1, 1.5, True])
def test_invalid_per_snapshot_element_bound_is_rejected(max_elements):
    with pytest.raises(ValueError):
        ObservationStore(max_elements_per_snapshot=max_elements)


@pytest.mark.parametrize("ttl_seconds", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_ttl_is_rejected(ttl_seconds: float):
    with pytest.raises(ValueError):
        ObservationStore(ttl_seconds=ttl_seconds)


@pytest.mark.parametrize("local_id", [-1, 1.5, True, "1"])
def test_invalid_local_element_id_is_rejected(local_id):
    store = ObservationStore()

    with pytest.raises(ValueError):
        store.create(
            provider="native",
            mode=OperationMode.FOREGROUND,
            target_id="target",
            generation=1,
            revision=1,
            elements=[ElementRecord(local_id=local_id, value="value")],
        )
