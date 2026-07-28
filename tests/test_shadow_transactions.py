from __future__ import annotations

import asyncio
import hashlib
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_eyes.cdp import CDPClient, SecureDOMMetadata
from agent_eyes.cdp_persistent import CDPSession
from agent_eyes.adapters.base import UIElement
from agent_eyes.observations import ElementRecord, ObservationStore
from agent_eyes.operation import OperationError, OperationErrorCode, OperationMode
from agent_eyes.target_resolver import (
    InventoryCacheStatus,
    ProviderTarget,
    ResolutionSource,
    ResolvedTarget,
    TargetResolution,
)
from agent_eyes.transaction_contract import TargetMode, parse_execute_request
from agent_eyes.transactions import TransactionEngine, TransactionStatus


class _TreeClient:
    def __init__(self) -> None:
        self._client = CDPClient()
        self.secure_scans = 0

    async def collect_secure_dom_metadata(self, _send, _nodes):
        self.secure_scans += 1
        return SecureDOMMetadata(complete=True)

    def _build_tree(self, nodes, *, secure_metadata=None):
        return self._client._build_tree(nodes, secure_metadata=secure_metadata)


class _ScenarioSession(CDPSession):
    def __init__(
        self,
        *,
        target_id: str = "target-1",
        scenario: str = "click",
        click_timeout: bool = False,
        box_model: dict | None = None,
        change_revision_after_box: bool = False,
        runtime_envelope: str = "",
        click_gate: tuple[asyncio.Event, asyncio.Event] | None = None,
        tree_revision_changes: int = 0,
        extra_nodes: int = 0,
        action_role: str = "",
        action_name: str = "",
    ) -> None:
        super().__init__(
            "session-1",
            MagicMock(),
            target_id=target_id,
            generation=7,
        )
        self.scenario = scenario
        self.phase = "initial"
        self.revision = 11
        self.click_timeout = click_timeout
        self.box_model = (
            {"model": {"content": [10, 10, 50, 10, 50, 30, 10, 30]}}
            if box_model is None
            else box_model
        )
        self.change_revision_after_box = change_revision_after_box
        self.runtime_envelope = runtime_envelope
        self.click_gate = click_gate
        self.tree_revision_changes = tree_revision_changes
        self.extra_nodes = extra_nodes
        self.action_role = action_role
        self.action_name = action_name
        self.enabled_domains: list[str] = []
        self.calls: list[tuple[str, dict, bool]] = []
        self.event_armed_at_click = False
        self.click_attempts = 0
        self.focus_attempts = 0
        self.focus_protocol_sent = False
        self.inserted_text: list[str] = []

    async def enable_domain(self, domain: str) -> None:
        if domain not in self.enabled_domains:
            self.enabled_domains.append(domain)

    def _nodes(self) -> list[dict]:
        if self.phase == "posted":
            child = {
                "nodeId": "posted",
                "ignored": False,
                "role": {"value": "article"},
                "name": {"value": "Comment posted"},
                "backendDOMNodeId": 303,
                "childIds": [],
                "properties": [],
            }
        elif self.scenario in {"type", "key"}:
            child = {
                "nodeId": "editor",
                "ignored": False,
                "role": {"value": "textbox"},
                "name": {"value": "Comment"},
                "value": {"value": ""},
                "backendDOMNodeId": 202,
                "childIds": [],
                "properties": [],
            }
        else:
            child = {
                "nodeId": "button",
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": "Add comment"},
                "backendDOMNodeId": 101,
                "childIds": [],
                "properties": [],
            }
        extras = [
            {
                "nodeId": f"extra-{index}",
                "ignored": False,
                "role": {"value": "article"},
                "name": {"value": f"Diff row {index}"},
                "backendDOMNodeId": 1_000 + index,
                "childIds": [],
                "properties": [],
            }
            for index in range(self.extra_nodes)
        ]
        return [
            {
                "nodeId": "root",
                "ignored": False,
                "role": {"value": "RootWebArea"},
                "name": {"value": "Pull request"},
                "childIds": [child["nodeId"], *(node["nodeId"] for node in extras)],
                "properties": [],
            },
            child,
            *extras,
        ]

    def _publish_posted(self) -> None:
        self.phase = "posted"
        self.revision += 1
        self._on_message(
            {"method": "Accessibility.nodesUpdated", "params": {"nodes": []}}
        )

    async def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        idempotent: bool = False,
    ) -> dict:
        supplied = params or {}
        self.calls.append((method, supplied, idempotent))
        if method == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {
                        "id": "frame-1",
                        "loaderId": f"loader-{self.revision}",
                    }
                }
            }
        if method == "DOM.getDocument":
            return {"root": {"backendNodeId": self.revision}}
        if method == "Accessibility.getFullAXTree":
            nodes = self._nodes()
            if self.tree_revision_changes > 0:
                self.tree_revision_changes -= 1
                self.revision += 1
            return {"nodes": nodes}
        if method == "Accessibility.getPartialAXTree":
            observed = self._nodes()[1]
            if self.runtime_envelope in {"click-stale", "focus-stale"}:
                return {"nodes": []}
            if (
                self.focus_protocol_sent
                and self.runtime_envelope == "focus-ax-malformed"
            ):
                return {"nodes": "malformed"}
            properties = list(observed.get("properties", []))
            if self.focus_protocol_sent and self.runtime_envelope != "focus-mismatch":
                properties.append(
                    {
                        "name": "focused",
                        "value": {"type": "booleanOrUndefined", "value": True},
                    }
                )
            return {
                "nodes": [
                    {
                        **observed,
                        "role": {
                            "value": self.action_role
                            or observed.get("role", {}).get("value", "")
                        },
                        "name": {
                            "value": self.action_name
                            or observed.get("name", {}).get("value", "")
                        },
                        "properties": properties,
                    }
                ]
            }
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"object-{supplied['backendNodeId']}"}}
        if method == "DOM.getBoxModel":
            result = self.box_model
            if self.change_revision_after_box:
                self.revision += 1
            return result
        if method == "Runtime.callFunctionOn":
            declaration = supplied.get("functionDeclaration", "")
            if "this.click()" in declaration:
                self.click_attempts += 1
                self.event_armed_at_click = bool(
                    self._event_handlers.get("Accessibility.nodesUpdated")
                )
                if self.runtime_envelope == "click-stale":
                    raise AssertionError("stale click reached Runtime dispatch")
                if self.runtime_envelope == "click-spoofed-stale":
                    return {
                        "exceptionDetails": {
                            "exception": {"description": "Error: STALE_ELEMENT"}
                        }
                    }
                if self.runtime_envelope == "click-generic":
                    return {"exceptionDetails": {"text": "Uncaught"}}
                if self.click_gate is not None:
                    started, release = self.click_gate
                    started.set()
                    await release.wait()
                if self.click_timeout:
                    self._publish_posted()
                    raise TimeoutError("provider response was lost")
                asyncio.get_running_loop().call_later(0.001, self._publish_posted)
                return {"result": {"value": "__agent_eyes_click_applied_v1__"}}
            return {}
        if method == "DOM.focus":
            self.focus_attempts += 1
            self.focus_protocol_sent = True
            if self.runtime_envelope == "focus-generic":
                return {"unexpected": True}
            return {}
        if method == "Input.insertText":
            self.inserted_text.append(str(supplied["text"]))
            return {}
        if method in {"Input.dispatchKeyEvent", "Input.dispatchMouseEvent"}:
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")


def _request(*, scenario: str, text: str = "", snapshot: str = ""):
    if scenario in {"click", "hover", "locate"}:
        steps = [
            {
                "op": "locate",
                "as": "comment",
                "role": "button",
                "name": "Add comment",
            },
        ]
        if scenario == "hover":
            steps.append({"op": "hover", "ref": "comment"})
        elif scenario == "click":
            steps.append(
                {
                    "op": "click",
                    "ref": "comment",
                    "expect": {"role": "article", "name": "Comment posted"},
                }
            )
    elif scenario == "type":
        steps = [
            {
                "op": "locate",
                "as": "editor",
                "role": "textbox",
                "name": "Comment",
            },
            {"op": "type", "ref": "editor", "text": text},
        ]
    elif scenario == "key":
        steps = [
            {
                "op": "locate",
                "as": "editor",
                "role": "textbox",
                "name": "Comment",
            },
            {"op": "press_key", "ref": "editor", "key": "Enter"},
        ]
    elif scenario == "scroll":
        steps = [{"op": "scroll", "delta_y": 300}]
    else:
        raise AssertionError(scenario)
    target = {"target_id": "target-1", "mode": "shadow"}
    if snapshot:
        target["snapshot"] = snapshot
    return parse_execute_request(
        {
            "target": target,
            "steps": steps,
            "deadline_ms": 1_000,
        }
    )


def _resolution(provider: str, target_id: str = "target-1") -> TargetResolution:
    tab = SimpleNamespace(
        id=target_id,
        title="Pull request",
        url="https://example.test/pr/1",
    )
    provider_target = ProviderTarget(
        target_id=target_id,
        value=(provider, tab),
    )
    return TargetResolution(
        target=ResolvedTarget(
            mode=TargetMode.SHADOW,
            target_id=target_id,
            pid=None,
            source=ResolutionSource.EXACT,
            provider_target=provider_target,
        ),
        cache_status=InventoryCacheStatus.MISS,
        activated=False,
    )


def _runtime(
    request,
    session: _ScenarioSession,
    *,
    provider: str = "persistent",
    store: ObservationStore | None = None,
):
    from agent_eyes.shadow_transactions import PersistentCDPTransactionRuntime

    resolver_calls: list[tuple[object, bool]] = []

    async def resolve(target, *, activate, budget):
        budget.checkpoint("fake resolution")
        resolver_calls.append((target, activate))
        return _resolution(provider)

    store = store or ObservationStore(
        token_factory=iter([f"s{index}" for index in range(20)]).__next__
    )
    runtime = PersistentCDPTransactionRuntime(
        request,
        resolver=resolve,
        session_for_target=lambda target_id: (
            session if target_id == session.target_id else None
        ),
        observation_store=store,
        cdp_client=_TreeClient(),
    )
    return runtime, store, resolver_calls


def _revision(identity: int) -> int:
    digest = hashlib.blake2b(
        f"loader-{identity}:{identity}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big")


def test_persistent_shadow_click_arms_event_before_one_dispatch_and_refreshes():
    async def run():
        request = _request(scenario="click")
        session = _ScenarioSession()
        runtime, store, resolver_calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        assert result.completed_steps == 2
        assert result.final_expectation is None
        assert session.click_attempts == 1
        assert session.event_armed_at_click is True
        assert session._event_handlers == {}
        assert resolver_calls == [(request.target, False)]
        assert (
            sum(
                method == "Accessibility.getFullAXTree"
                for method, _, _ in session.calls
            )
            >= 2
        )
        assert all(
            idempotent is False
            for method, _, idempotent in session.calls
            if method == "Runtime.callFunctionOn"
        )
        snapshot = store.get_snapshot(result.snapshot)
        assert snapshot.provider == "cdp-persistent"
        assert snapshot.target_id == "target-1"
        assert snapshot.generation == 7

    asyncio.run(run())


def test_current_persistent_snapshot_binds_target_but_refreshes_ax_semantics():
    async def run():
        session = _ScenarioSession(scenario="type")
        store = ObservationStore(token_factory=lambda: "existing")
        editor = UIElement(
            id=2,
            role="textbox",
            name="Comment",
            platform_ref=202,
            source="cdp",
        )
        root = UIElement(
            id=1,
            role="RootWebArea",
            name="Pull request",
            children=[editor],
            source="cdp",
        )
        snapshot = store.create(
            provider="cdp-persistent",
            mode=OperationMode.SHADOW,
            target_id="target-1",
            generation=7,
            revision=_revision(11),
            elements=(
                ElementRecord(local_id=root.id, value=root, actionable=False),
                ElementRecord(local_id=editor.id, value=editor),
            ),
        )
        request = _request(
            scenario="type",
            text="private-inline-comment",
            snapshot=snapshot.token,
        )
        runtime, _store, _calls = _runtime(request, session, store=store)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        assert session.inserted_text == ["private-inline-comment"]
        assert any(
            method == "Accessibility.getFullAXTree" for method, _, _ in session.calls
        )

    asyncio.run(run())


@pytest.mark.parametrize("quad_name", ["content", "border"])
def test_persistent_shadow_hover_uses_one_box_preflight_and_one_mouse_dispatch(
    quad_name,
):
    async def run():
        request = _request(scenario="hover")
        session = _ScenarioSession(
            scenario="hover",
            box_model={"model": {quad_name: [10, 10, 50, 10, 50, 30, 10, 30]}},
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        boxes = [
            (params, idempotent)
            for method, params, idempotent in session.calls
            if method == "DOM.getBoxModel"
        ]
        moves = [
            (params, idempotent)
            for method, params, idempotent in session.calls
            if method == "Input.dispatchMouseEvent"
        ]
        assert boxes == [({"backendNodeId": 101}, True)]
        assert moves == [({"type": "mouseMoved", "x": 30, "y": 20}, False)]

    asyncio.run(run())


def test_revision_bracket_retries_one_changed_initial_observation():
    async def run():
        request = _request(scenario="locate")
        session = _ScenarioSession(tree_revision_changes=1)
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        assert (
            sum(
                method == "Accessibility.getFullAXTree"
                for method, _, _ in session.calls
            )
            == 2
        )

    asyncio.run(run())


def test_large_ax_tree_keeps_a_bounded_snapshot_without_failing_transaction():
    async def run():
        request = _request(scenario="locate")
        session = _ScenarioSession(extra_nodes=600)
        runtime, store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        snapshot = store.get_snapshot(result.snapshot)
        assert len(snapshot.elements) == 500

    asyncio.run(run())


def test_persistent_shadow_hover_rejects_stale_geometry_before_dispatch():
    async def run():
        request = _request(scenario="hover")
        session = _ScenarioSession(
            scenario="hover",
            change_revision_after_box=True,
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.STALE_SNAPSHOT
        assert result.retry_safe is True
        assert not any(
            method == "Input.dispatchMouseEvent" for method, _, _ in session.calls
        )

    asyncio.run(run())


def test_persistent_shadow_hover_rejects_missing_geometry_before_dispatch():
    async def run():
        request = _request(scenario="hover")
        session = _ScenarioSession(scenario="hover", box_model={"model": {}})
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert result.retry_safe is True
        assert not any(
            method == "Input.dispatchMouseEvent" for method, _, _ in session.calls
        )

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["type", "key", "scroll"])
def test_persistent_shadow_actions_use_one_selected_provider_dispatch(scenario):
    async def run():
        secret = "private-inline-comment"
        request = _request(scenario=scenario, text=secret)
        session = _ScenarioSession(scenario=scenario)
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.SUCCEEDED
        if scenario == "type":
            assert session.focus_attempts == 1
            assert session.inserted_text == [secret]
            assert secret not in repr(result)
        elif scenario == "key":
            assert session.focus_attempts == 1
            key_events = [
                params["type"]
                for method, params, _ in session.calls
                if method == "Input.dispatchKeyEvent"
            ]
            assert key_events == ["keyDown", "keyUp"]
        else:
            wheel = [
                params
                for method, params, _ in session.calls
                if method == "Input.dispatchMouseEvent"
            ]
            assert wheel == [
                {
                    "type": "mouseWheel",
                    "x": 400,
                    "y": 400,
                    "deltaX": 0,
                    "deltaY": 300,
                }
            ]

    asyncio.run(run())


def test_post_dispatch_provider_failure_is_unknown_and_never_replayed():
    async def run():
        request = _request(scenario="click")
        session = _ScenarioSession(click_timeout=True)
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.retry_safe is False
        assert session.click_attempts == 1
        assert session._event_handlers == {}

    asyncio.run(run())


def test_cancellation_during_shadow_dispatch_is_unknown_and_cleans_resources():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        request = _request(scenario="click")
        session = _ScenarioSession(click_gate=(started, release))
        runtime, store, _calls = _runtime(request, session)
        task = asyncio.create_task(
            TransactionEngine().run(request, ports=runtime.ports())
        )
        await started.wait()

        task.cancel()
        result = await task

        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.retry_safe is False
        assert session.click_attempts == 1
        assert session._event_handlers == {}
        with pytest.raises(OperationError) as stale:
            store.get_snapshot("s0")
        assert stale.value.code is OperationErrorCode.STALE_SNAPSHOT

    asyncio.run(run())


@pytest.mark.parametrize(
    ("scenario", "runtime_envelope"),
    [("click", "click-stale"), ("type", "focus-stale")],
)
def test_known_stale_ax_preflight_stops_before_later_mutation(
    scenario,
    runtime_envelope,
):
    async def run():
        request = _request(scenario=scenario, text="must-not-be-sent")
        session = _ScenarioSession(
            scenario=scenario,
            runtime_envelope=runtime_envelope,
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.STALE_SNAPSHOT
        assert result.retry_safe is True
        assert session.inserted_text == []
        assert session._event_handlers == {}

    asyncio.run(run())


@pytest.mark.parametrize(
    ("scenario", "runtime_envelope"),
    [("click", "click-spoofed-stale")],
)
def test_page_exception_cannot_forge_retry_safe_stale(scenario, runtime_envelope):
    async def run():
        request = _request(scenario=scenario, text="must-not-be-sent")
        session = _ScenarioSession(
            scenario=scenario,
            runtime_envelope=runtime_envelope,
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.retry_safe is False
        assert session.inserted_text == []

    asyncio.run(run())


def test_focus_mismatch_is_retry_safe_and_never_sends_text():
    async def run():
        request = _request(scenario="type", text="must-not-be-sent")
        session = _ScenarioSession(
            scenario="type",
            runtime_envelope="focus-mismatch",
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.FOCUS_MISMATCH
        assert result.retry_safe is True
        assert session.inserted_text == []

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["click", "type"])
def test_changed_exact_node_accessible_name_stops_before_action(scenario):
    async def run():
        request = _request(scenario=scenario, text="must-not-be-sent")
        session = _ScenarioSession(scenario=scenario, action_name="Delete")
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.STALE_SNAPSHOT
        assert result.retry_safe is True
        assert session.click_attempts == 0
        assert session.focus_attempts == 0
        assert session.inserted_text == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("scenario", "runtime_envelope"),
    [("click", "click-generic"), ("type", "focus-generic")],
)
def test_unknown_runtime_envelope_stops_without_replay(scenario, runtime_envelope):
    async def run():
        request = _request(scenario=scenario, text="must-not-be-sent")
        session = _ScenarioSession(
            scenario=scenario,
            runtime_envelope=runtime_envelope,
        )
        runtime, _store, _calls = _runtime(request, session)

        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.retry_safe is False
        assert session.inserted_text == []
        if scenario == "click":
            assert session.click_attempts == 1
        assert session._event_handlers == {}

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["legacy", "apple-events", "unknown"])
def test_non_persistent_shadow_providers_are_rejected_before_session_use(provider):
    async def run():
        from agent_eyes.shadow_transactions import PersistentCDPTransactionRuntime

        request = _request(scenario="scroll")
        session_calls = 0

        async def resolve(_target, *, activate, budget):
            assert activate is False
            budget.checkpoint("fake resolution")
            return _resolution(provider)

        def session_for_target(_target_id):
            nonlocal session_calls
            session_calls += 1
            raise AssertionError("unsupported provider acquired a CDP session")

        runtime = PersistentCDPTransactionRuntime(
            request,
            resolver=resolve,
            session_for_target=session_for_target,
            observation_store=ObservationStore(),
            cdp_client=_TreeClient(),
        )
        result = await TransactionEngine().run(request, ports=runtime.ports())

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert result.completed_steps == 0
        assert session_calls == 0

    asyncio.run(run())


def test_shadow_runtime_has_no_server_or_coordinator_dependency():
    from agent_eyes import shadow_transactions

    source = inspect.getsource(shadow_transactions)

    assert "from .server" not in source
    assert "import server" not in source
    assert "AutomationCoordinator" not in source
    assert "execute_shadow" not in source
