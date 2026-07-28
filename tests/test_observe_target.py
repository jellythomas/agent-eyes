from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.observations import ObservationStore
from agent_eyes.operation import OperationBudget, OperationError, OperationErrorCode
from agent_eyes.target_observation import (
    ObservationLoadKind,
    SelectorMatchStatus,
    TargetObservationService,
)
from agent_eyes.target_resolver import TargetResolver
from agent_eyes.transaction_contract import (
    Locator,
    MatchMode,
    ObserveTargetRequest,
    TargetIntent,
    TargetMode,
    TargetSpec,
)


def _request(
    target: TargetSpec,
    *,
    intent: TargetIntent = TargetIntent.INSPECT,
    selectors: tuple[Locator, ...] = (),
    max_results: int = 10,
    deadline_ms: int = 3_000,
) -> ObserveTargetRequest:
    return ObserveTargetRequest(
        target=target,
        intent=intent,
        selectors=selectors,
        max_results=max_results,
        deadline_ms=deadline_ms,
    )


def _tree() -> tuple[UIElement, dict[str, UIElement]]:
    editor = UIElement(
        id=3,
        role="textbox",
        name="Comment editor",
        states=["enabled"],
        actions=["set_value"],
    )
    save = UIElement(
        id=4,
        role="button",
        name="Save comment",
        states=["enabled"],
        actions=["press"],
    )
    root = UIElement(
        id=1,
        role="window",
        name="Pull request",
        children=[
            UIElement(
                id=2,
                role="group",
                name="Inline comment",
                children=[editor, save],
            )
        ],
    )
    return root, {"root": root, "editor": editor, "save": save}


def _service(
    resolver: TargetResolver,
    store: ObservationStore,
    *,
    tree_loader,
    subtree_loader,
    target_validator=None,
    max_snapshot_elements: int = 500,
) -> TargetObservationService:
    return TargetObservationService(
        resolver,
        store,
        tree_loader=tree_loader,
        subtree_loader=subtree_loader,
        provider="native-test",
        target_validator=target_validator,
        revision_factory=lambda: 77,
        max_depth=10,
        max_snapshot_elements=max_snapshot_elements,
    )


def test_pid_inspection_loads_one_tree_without_inventory_or_activation() -> None:
    async def run() -> None:
        provider = object()
        adapter = object()
        store = ObservationStore(token_factory=lambda: "nsnapshot")
        tree, elements = _tree()
        tree_calls: list[tuple[object, int, int, OperationBudget]] = []

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("an exact PID must bypass browser inventory")

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspect must not activate")

        async def load_tree(adapter_identity, pid, max_depth, budget):
            tree_calls.append((adapter_identity, pid, max_depth, budget))
            return tree

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("a PID target must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            store,
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=(
                    Locator(role="textbox"),
                    Locator(
                        role="button",
                        name="save",
                        match=MatchMode.PREFIX,
                    ),
                ),
            ),
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert len(tree_calls) == 1
        assert tree_calls[0][:3] == (adapter, 73, 10)
        assert 0 < tree_calls[0][3].remaining() <= 3.0
        assert result.target.target_id == "pid:73"
        assert result.target.activated is False
        assert result.scan.kind is ObservationLoadKind.TREE
        assert result.scan.nodes == 4
        assert result.scan.inventory_cache == "bypass"
        assert result.selectors[0].matches[0].local_id == elements["editor"].id
        assert result.selectors[1].matches[0].local_id == elements["save"].id
        assert result.snapshot.token == "nsnapshot"
        stored_save = store.resolve("nsnapshot", elements["save"].id).value
        assert stored_save is not elements["save"]
        assert stored_save.name == elements["save"].name
        assert stored_save.children == []
        assert all(element.pid == 73 for element in result.index.elements)

    asyncio.run(run())


def test_caller_supplied_budget_bounds_the_complete_observation() -> None:
    async def run() -> None:
        tree_calls = 0

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation must bypass inventory")

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspection must not activate")

        async def load_tree(_adapter, _pid, _depth, _budget):
            nonlocal tree_calls
            tree_calls += 1
            return _tree()[0]

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        with pytest.raises(OperationError) as exc_info:
            await service.observe(
                _request(TargetSpec(mode=TargetMode.FOREGROUND, pid=73)),
                provider_identity=object(),
                adapter_identity=object(),
                budget=OperationBudget.start(0.0),
            )

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert tree_calls == 0

    asyncio.run(run())


def test_browser_inspection_refreshes_exact_window_subtree_once() -> None:
    async def run() -> None:
        provider = object()
        adapter = object()
        window = UIElement(id=90, role="window", platform_ref=object())
        target = BrowserTarget(
            browser="Firefox",
            pid=41,
            title="Agent Eyes PR",
            url="https://bitbucket.example/secret-project/pull-requests/42",
            window_index=2,
            tab_index=4,
            window_element=window,
        )
        tree, _ = _tree()
        subtree_calls: list[tuple[object, UIElement, int]] = []
        activation_calls = 0

        async def inventory(_provider, _adapter, mode):
            assert mode is TargetMode.FOREGROUND
            return [target]

        async def activate(_provider, _adapter, _target):
            nonlocal activation_calls
            activation_calls += 1
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            raise AssertionError("a browser target must use its exact window subtree")

        async def load_subtree(adapter_identity, live_window, max_depth, _budget):
            subtree_calls.append((adapter_identity, live_window, max_depth))
            return tree

        async def validate_target(_adapter, _resolution, _budget):
            return True

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[target],
        )
        service = _service(
            resolver,
            ObservationStore(token_factory=lambda: "nbrowser"),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
            target_validator=validate_target,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, target_id=target.identifier),
                selectors=(Locator(role="button"),),
            ),
            provider_identity=provider,
            adapter_identity=adapter,
        )

        assert subtree_calls == [(adapter, window, 10)]
        assert activation_calls == 0
        assert result.target.target_id == target.identifier
        assert result.scan.kind is ObservationLoadKind.SUBTREE
        assert "secret-project" not in result.to_json()

    asyncio.run(run())


def test_browser_inspection_revalidates_identity_after_subtree_load() -> None:
    async def run() -> None:
        provider = object()
        adapter = object()
        window = UIElement(id=90, role="window", platform_ref=object())
        target = BrowserTarget(
            browser="Firefox",
            pid=41,
            title="Target A",
            window_index=0,
            tab_index=0,
            window_element=window,
        )
        validations = iter((True, False))
        loads = 0
        store = ObservationStore()

        async def inventory(_provider, _adapter, _mode):
            return [target]

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspect must not activate")

        async def load_tree(_adapter, _pid, _depth, _budget):
            raise AssertionError("browser observation must use its exact subtree")

        async def load_subtree(_adapter, _window, _depth, _budget):
            nonlocal loads
            loads += 1
            return UIElement(id=2, role="button", name="Target B only")

        async def validate_target(_adapter, _resolution, _budget):
            return next(validations)

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[target],
        )
        service = _service(
            resolver,
            store,
            tree_loader=load_tree,
            subtree_loader=load_subtree,
            target_validator=validate_target,
        )

        with pytest.raises(OperationError) as exc_info:
            await service.observe(
                _request(
                    TargetSpec(mode=TargetMode.FOREGROUND, target_id=target.identifier),
                    selectors=(Locator(role="button", name="Target B only"),),
                ),
                provider_identity=provider,
                adapter_identity=adapter,
            )

        assert exc_info.value.code is OperationErrorCode.FOCUS_MISMATCH
        assert loads == 1
        assert store._snapshots == {}

    asyncio.run(run())


def test_interact_delegates_exactly_one_activation_to_the_resolver() -> None:
    async def run() -> None:
        window = UIElement(id=8, role="window", platform_ref=object())
        target = BrowserTarget(
            browser="Safari",
            pid=51,
            title="Pull request",
            window_index=0,
            tab_index=0,
            window_element=window,
        )
        activated = []
        subtree_calls = 0

        async def inventory(_provider, _adapter, _mode):
            return [target]

        async def activate(_provider, _adapter, resolved):
            activated.append(resolved)
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            raise AssertionError("browser observation must use get_subtree")

        async def load_subtree(_adapter, live_window, _depth, _budget):
            nonlocal subtree_calls
            subtree_calls += 1
            assert live_window is window
            return _tree()[0]

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, query="pull request"),
                intent=TargetIntent.INTERACT,
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )

        assert len(activated) == 1
        assert activated[0].target_id == target.identifier
        assert subtree_calls == 1
        assert result.target.activated is True

    asyncio.run(run())


def test_missing_live_browser_window_fails_without_tree_fallback() -> None:
    async def run() -> None:
        provider = object()
        adapter = object()
        target = BrowserTarget(
            browser="Firefox",
            pid=41,
            title="Pull request",
            window_index=0,
            tab_index=0,
        )
        loads = 0

        async def inventory(_provider, _adapter, _mode):
            return [target]

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            nonlocal loads
            loads += 1
            return _tree()[0]

        async def load_subtree(_adapter, _window, _depth, _budget):
            nonlocal loads
            loads += 1
            return _tree()[0]

        async def validate_target(_adapter, _resolution, _budget):
            return True

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[target],
        )
        service = _service(
            resolver,
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
            target_validator=validate_target,
        )

        with pytest.raises(OperationError) as exc_info:
            await service.observe(
                _request(
                    TargetSpec(
                        mode=TargetMode.FOREGROUND,
                        target_id=target.identifier,
                    )
                ),
                provider_identity=provider,
                adapter_identity=adapter,
            )

        assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT
        assert loads == 0

    asyncio.run(run())


def test_background_browser_inspection_fails_without_loading_visible_window() -> None:
    async def run() -> None:
        provider = object()
        adapter = object()
        window = UIElement(id=90, role="window", platform_ref=object())
        target = BrowserTarget(
            browser="Firefox",
            pid=41,
            title="Background pull request",
            window_index=2,
            tab_index=4,
            window_element=window,
        )
        loads = 0

        async def inventory(_provider, _adapter, _mode):
            return [target]

        async def activate(_provider, _adapter, _target):
            raise AssertionError("inspect must preserve focus")

        async def load_tree(_adapter, _pid, _depth, _budget):
            nonlocal loads
            loads += 1
            return _tree()[0]

        async def load_subtree(_adapter, _window, _depth, _budget):
            nonlocal loads
            loads += 1
            return _tree()[0]

        async def validate_target(_adapter, _resolution, _budget):
            return False

        resolver = TargetResolver(inventory, activate)
        resolver.remember_targets(
            provider_identity=provider,
            adapter_identity=adapter,
            mode=TargetMode.FOREGROUND,
            targets=[target],
        )
        service = _service(
            resolver,
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
            target_validator=validate_target,
        )

        with pytest.raises(OperationError) as exc_info:
            await service.observe(
                _request(
                    TargetSpec(
                        mode=TargetMode.FOREGROUND,
                        target_id=target.identifier,
                    )
                ),
                provider_identity=provider,
                adapter_identity=adapter,
            )

        assert exc_info.value.code is OperationErrorCode.FOCUS_MISMATCH
        assert loads == 0

    asyncio.run(run())


def test_missing_provider_tree_fails_after_one_scan_without_retry() -> None:
    async def run() -> None:
        calls = 0

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            nonlocal calls
            calls += 1
            return None

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )

        with pytest.raises(OperationError) as exc_info:
            await service.observe(
                _request(TargetSpec(mode=TargetMode.FOREGROUND, pid=44)),
                provider_identity=object(),
                adapter_identity=object(),
            )

        assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND
        assert calls == 1

    asyncio.run(run())


def test_all_selectors_use_one_frozen_locator_index(monkeypatch) -> None:
    async def run() -> None:
        import agent_eyes.target_observation as observation_module

        real_index = observation_module.LocatorIndex
        calls = {"build": 0, "batch": 0}

        class CountingIndex:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            @classmethod
            def from_roots(cls, roots):
                calls["build"] += 1
                return cls(real_index.from_roots(roots))

            @property
            def elements(self):
                return self._wrapped.elements

            def find_many(self, selectors):
                calls["batch"] += 1
                return self._wrapped.find_many(selectors)

        monkeypatch.setattr(observation_module, "LocatorIndex", CountingIndex)

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return _tree()[0]

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=(
                    Locator(role="textbox"),
                    Locator(role="button"),
                    Locator(name="comment", match=MatchMode.CONTAINS),
                ),
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )

        assert calls == {"build": 1, "batch": 1}

    asyncio.run(run())


def test_ambiguous_selector_returns_bounded_candidates_without_selecting_one() -> None:
    async def run() -> None:
        root = UIElement(
            id=1,
            role="window",
            children=[
                UIElement(id=2, role="button", name="Save comment"),
                UIElement(id=3, role="button", name="Save comment"),
            ],
        )

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return root

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=(Locator(role="button"),),
                max_results=1,
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )

        matches = result.selectors[0]
        assert matches.status is SelectorMatchStatus.AMBIGUOUS
        assert matches.total == 2
        assert matches.truncated is True
        assert len(matches.matches) == 1
        assert "selected" not in result.to_dict()["selectors"][0]

    asyncio.run(run())


def test_output_contains_only_bounded_matched_metadata_and_redacts_secure_values() -> (
    None
):
    async def run() -> None:
        root = UIElement(
            id=1,
            role="window",
            description="unmatched-description-secret",
            children=[
                UIElement(
                    id=2,
                    role="statictext",
                    name="unmatched-name-secret",
                    value="unmatched-value-secret",
                ),
                UIElement(
                    id=3,
                    role="passwordfield",
                    name="Password",
                    value="matched-password-secret",
                    states=["secure", "focused"],
                    platform_ref=object(),
                ),
            ],
        )

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return root

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(token_factory=lambda: "nprivacy"),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=(Locator(role="passwordfield"),),
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )
        rendered = result.to_json()

        assert "Password" in rendered
        assert "matched-password-secret" not in rendered
        assert "unmatched-description-secret" not in rendered
        assert "unmatched-name-secret" not in rendered
        assert "unmatched-value-secret" not in rendered
        assert "platform_ref" not in rendered

    asyncio.run(run())


def test_compact_json_remains_valid_and_at_most_four_kibibytes() -> None:
    async def run() -> None:
        root = UIElement(
            id=1,
            role="window",
            children=[
                UIElement(
                    id=index + 2,
                    role="button",
                    name=f"Candidate {index} " + ("界" * 400),
                    value="値" * 400,
                    states=["enabled", "visible", "focused"],
                    actions=["press", "show_menu"],
                )
                for index in range(25)
            ],
        )

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return root

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=tuple(Locator(role="button") for _ in range(8)),
                max_results=20,
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )
        rendered = result.to_json()
        payload = json.loads(rendered)

        assert len(rendered.encode("utf-8")) <= 4 * 1024
        assert payload["truncated"] is True
        assert payload["status"] == "ok"
        assert len(payload["selectors"]) == 8

    asyncio.run(run())


def test_snapshot_and_result_metadata_are_immutable() -> None:
    async def run() -> None:
        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return _tree()[0]

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            ObservationStore(),
            tree_loader=load_tree,
            subtree_loader=load_subtree,
        )
        result = await service.observe(
            _request(TargetSpec(mode=TargetMode.FOREGROUND, pid=73)),
            provider_identity=object(),
            adapter_identity=object(),
        )

        with pytest.raises(FrozenInstanceError):
            result.snapshot.revision = 78
        with pytest.raises(FrozenInstanceError):
            result.target.target_id = "pid:99"

    asyncio.run(run())


def test_snapshot_cap_limits_actionable_records_and_reports_scan_truncation() -> None:
    async def run() -> None:
        root = UIElement(
            id=1,
            role="window",
            children=[
                UIElement(id=index + 2, role="button", name=f"Button {index}")
                for index in range(8)
            ],
        )
        store = ObservationStore(
            max_elements_per_snapshot=4,
            token_factory=lambda: "ncapped",
        )

        async def inventory(_provider, _adapter, _mode):
            raise AssertionError("PID observation bypasses inventory")

        async def activate(_provider, _adapter, _target):
            return True

        async def load_tree(_adapter, _pid, _depth, _budget):
            return root

        async def load_subtree(_adapter, _window, _depth, _budget):
            raise AssertionError("PID observation must not load a subtree")

        service = _service(
            TargetResolver(inventory, activate),
            store,
            tree_loader=load_tree,
            subtree_loader=load_subtree,
            max_snapshot_elements=4,
        )
        result = await service.observe(
            _request(
                TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
                selectors=(Locator(role="button"),),
            ),
            provider_identity=object(),
            adapter_identity=object(),
        )

        assert result.scan.nodes == 4
        assert result.scan.available_nodes == 9
        assert result.scan.truncated is True
        assert len(result.snapshot.elements) == 4
        assert [match.local_id for match in result.selectors[0].matches] == [2, 3, 4]

    asyncio.run(run())
