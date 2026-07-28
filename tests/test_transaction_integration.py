from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_eyes.adapters.base import AppInfo, UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.cdp import SecureDOMMetadata
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.native_events import run_native_action_until
from agent_eyes.observations import ElementRecord
from agent_eyes.operation import (
    OperationBudget,
    OperationError,
    OperationErrorCode,
    OperationMode,
)
from agent_eyes.target_resolver import (
    InventoryCacheStatus,
    ResolvedTarget,
    ResolutionSource,
    TargetResolution,
)
from agent_eyes.transaction_contract import (
    TargetMode,
    TargetSpec,
    parse_execute_request,
)
from agent_eyes.transactions import TransactionEngine


class _InlineCommentAdapter:
    def __init__(self) -> None:
        self.phase = "initial"
        self.full_scans = 0
        self.scoped_scans = 0
        self.actions: list[str] = []
        self.focused: UIElement | None = None

    def _tree(self) -> UIElement:
        if self.phase == "initial":
            children = [
                UIElement(
                    id=2,
                    role="button",
                    name="Add inline comment",
                    actions=["press"],
                    platform_ref=object(),
                )
            ]
        elif self.phase in {"editor", "typed"}:
            children = [
                UIElement(
                    id=3,
                    role="textbox",
                    name="Comment",
                    actions=["scrolltovisible"],
                    platform_ref=object(),
                ),
                UIElement(
                    id=4,
                    role="button",
                    name="Save",
                    actions=["press"],
                    platform_ref=object(),
                ),
            ]
        else:
            children = [
                UIElement(
                    id=5,
                    role="article",
                    name="Posted inline comment",
                    platform_ref=object(),
                )
            ]
        return UIElement(
            id=1,
            role="window",
            name="Pull request",
            children=children,
            platform_ref=object(),
        )

    def get_tree(self, pid: int, max_depth: int = 10) -> UIElement:
        assert pid == 73
        assert max_depth == 10
        self.full_scans += 1
        return self._tree()

    def get_subtree(self, element: UIElement, max_depth: int = 10) -> UIElement:
        assert element.role == "window"
        assert max_depth == 10
        self.scoped_scans += 1
        return self._tree()

    def perform_action(self, element: UIElement, action: str) -> bool:
        assert action == "press"
        self.actions.append(element.name)
        if element.name == "Add inline comment":
            self.phase = "editor"
            return True
        if element.name == "Save":
            self.phase = "posted"
            return True
        return False

    def is_element_valid(self, element: UIElement) -> bool:
        return element.platform_ref is not None

    def focus_element(self, element: UIElement) -> bool:
        self.focused = element
        return True

    def get_focused_element(self) -> UIElement | None:
        return self.focused

    def is_same_element(self, first: UIElement, second: UIElement) -> bool:
        return first is second

    def list_apps(self):
        raise AssertionError("PID transactions must not inventory browser apps")


class _InlineCommentInput:
    def __init__(self, adapter: _InlineCommentAdapter) -> None:
        self._adapter = adapter
        self.typed: list[str] = []

    def is_available(self) -> bool:
        return True

    def is_frontmost(self, pid: int) -> bool:
        return pid == 73

    def type_text(self, text: str) -> bool:
        self.typed.append(text)
        self._adapter.phase = "typed"
        return True


def test_observe_target_public_call_scans_pid_tree_once_and_returns_compact_json(
    monkeypatch,
) -> None:
    from agent_eyes import server

    root = UIElement(
        id=1,
        role="window",
        name="Pull request",
        children=[UIElement(id=2, role="button", name="Add inline comment")],
    )
    adapter = MagicMock()
    adapter.get_tree.return_value = root
    adapter.list_apps.side_effect = AssertionError(
        "PID target must not inventory browsers"
    )
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(
        server.call_tool(
            "observe_target",
            {
                "pid": 73,
                "selectors": [
                    {"role": "button", "name": "inline", "match": "contains"}
                ],
            },
        )
    )

    payload = json.loads(result[0].text)
    assert payload["status"] == "ok"
    assert payload["target"]["id"] == "pid:73"
    assert payload["scan"]["kind"] == "tree"
    assert payload["selectors"][0]["status"] == "unique"
    assert payload["selectors"][0]["matches"][0]["name"] == "Add inline comment"
    assert len(result[0].text.encode("utf-8")) <= 4 * 1024
    adapter.get_tree.assert_called_once_with(73, max_depth=10)


def test_execute_public_call_completes_inline_comment_in_one_bounded_transaction(
    monkeypatch,
) -> None:
    from agent_eyes import server

    secret = "private inline review 7e4d"
    adapter = _InlineCommentAdapter()
    input_backend = _InlineCommentInput(adapter)
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    async def deterministic_native_events(*args, **kwargs):
        kwargs["subscription_factory"] = lambda _pid: None
        return await run_native_action_until(*args, **kwargs)

    monkeypatch.setattr(server, "run_native_action_until", deterministic_native_events)

    result = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {"pid": 73},
                "steps": [
                    {
                        "op": "locate",
                        "as": "comment_button",
                        "role": "button",
                        "name": "Add inline comment",
                    },
                    {
                        "op": "click",
                        "ref": "comment_button",
                        "expect": {"role": "textbox", "name": "Comment"},
                    },
                    {
                        "op": "locate",
                        "as": "editor",
                        "role": "textbox",
                        "name": "Comment",
                    },
                    {"op": "type", "ref": "editor", "text": secret},
                    {
                        "op": "locate",
                        "as": "submit",
                        "role": "button",
                        "name": "Save",
                    },
                    {
                        "op": "click",
                        "ref": "submit",
                        "consequence": "external_write",
                    },
                ],
                "expect": {"role": "article", "name": "Posted inline comment"},
                "deadline_ms": 3_000,
            },
        )
    )

    assert isinstance(result, list)
    payload = json.loads(result[0].text)
    assert payload == {
        "status": "succeeded",
        "target_id": "pid:73",
        "completed_steps": 6,
        "elapsed_ms": payload["elapsed_ms"],
        "retry_safe": False,
        "final_expectation": True,
        "snapshot": payload["snapshot"],
    }
    assert 0 <= payload["elapsed_ms"] <= 3_000
    assert payload["snapshot"].startswith("n")
    assert secret not in result[0].text
    assert adapter.full_scans == 1
    assert adapter.scoped_scans == 3
    assert adapter.actions == ["Add inline comment", "Save"]
    assert input_backend.typed == [secret]
    assert len(result[0].text.encode("utf-8")) <= 2 * 1024


def test_execute_pid_snapshot_reloads_full_scope_instead_of_binding_first_match(
    monkeypatch,
) -> None:
    from agent_eyes import server

    root = UIElement(
        id=1,
        role="window",
        name="Pull request",
        platform_ref=object(),
        children=[
            UIElement(
                id=2,
                role="button",
                name="Add inline comment",
                platform_ref=object(),
            )
        ],
    )
    adapter = MagicMock()
    adapter.get_tree.return_value = root
    adapter.get_subtree.return_value = root
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    observed = asyncio.run(
        server.call_tool(
            "observe_target",
            {
                "pid": 73,
                "selectors": [{"role": "button", "name": "Add inline comment"}],
            },
        )
    )
    snapshot = json.loads(observed[0].text)["snapshot"]["token"]
    executed = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {"pid": 73, "snapshot": snapshot},
                "steps": [
                    {
                        "op": "locate",
                        "as": "comment_button",
                        "role": "button",
                        "name": "Add inline comment",
                    }
                ],
            },
        )
    )

    assert json.loads(executed[0].text)["status"] == "succeeded"
    assert adapter.get_tree.call_count == 2
    adapter.get_tree.assert_called_with(73, max_depth=10)
    adapter.get_subtree.assert_not_called()


def test_execute_pid_match_only_snapshot_cannot_hide_sibling_elements(
    monkeypatch,
) -> None:
    from agent_eyes import server

    match = UIElement(
        id=2,
        role="button",
        name="Add inline comment",
        platform_ref=object(),
    )
    sibling = UIElement(
        id=3,
        role="textbox",
        name="Comment",
        platform_ref=object(),
    )
    root = UIElement(
        id=1,
        role="window",
        name="Pull request",
        platform_ref=object(),
        children=[match, sibling],
    )
    local_coordinator = AutomationCoordinator()
    snapshot = local_coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:73",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=match.id, value=match)],
    )
    adapter = MagicMock()
    adapter.get_tree.return_value = root
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_runtime_readiness", SimpleNamespace(core_ready=True))
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", local_coordinator)

    executed = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {"pid": 73, "snapshot": snapshot.token},
                "steps": [
                    {
                        "op": "expect",
                        "role": "textbox",
                        "name": "Comment",
                    }
                ],
            },
        )
    )

    assert json.loads(executed[0].text)["status"] == "succeeded"
    adapter.get_tree.assert_called_once_with(73, max_depth=10)
    adapter.get_subtree.assert_not_called()


def _open_request():
    return parse_execute_request(
        {
            "target": {
                "query": "Pull request 42",
                "url": "https://example.test/pull-requests/42",
                "on_missing": "open",
            },
            "steps": [{"op": "expect", "role": "heading", "name": "Pull request"}],
        }
    )


def _resolved_browser_target() -> TargetResolution:
    return TargetResolution(
        target=ResolvedTarget(
            mode=TargetMode.FOREGROUND,
            target_id="native:73:w0:t1:r0123456789abcdef",
            pid=73,
            source=ResolutionSource.QUERY,
        ),
        cache_status=InventoryCacheStatus.HIT,
        activated=True,
    )


class _InlineWorker:
    async def run(self, call, **_kwargs):
        return call()

    async def wait_until_idle(self) -> None:
        return None


def test_open_if_missing_uses_one_gate_then_one_resolution(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    observed = BrowserTarget(
        browser="Google Chrome",
        pid=73,
        title="Pull request 42",
        url=request.target.url,
    )
    open_gate = AsyncMock(
        return_value=server._TransactionOpenResult(
            action_dispatched=True,
            opened=True,
            observed_targets=(observed,),
            resolution_query=request.target.query,
        )
    )
    resolution = AsyncMock(return_value=_resolved_browser_target())
    cache = MagicMock()
    invalidate = MagicMock()
    monkeypatch.setattr(server, "_open_transaction_target_until_present", open_gate)
    monkeypatch.setattr(server, "_resolve_transaction_target", resolution)
    monkeypatch.setattr(server, "_get_transaction_target_resolver", lambda: cache)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", invalidate)

    target = asyncio.run(
        server._ForegroundTransactionRuntime(request).resolve(
            request.target,
            True,
            OperationBudget.start(1.0),
        )
    )

    assert target.target_id == _resolved_browser_target().target.target_id
    assert target.replay_unsafe is True
    open_gate.assert_awaited_once()
    resolution.assert_awaited_once()
    cache.prime_inventory.assert_called_once()
    invalidate.assert_called_once()


def test_post_open_resolution_failure_is_always_outcome_unknown(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    monkeypatch.setattr(
        server,
        "_open_transaction_target_until_present",
        AsyncMock(
            return_value=server._TransactionOpenResult(
                action_dispatched=True,
                opened=True,
            )
        ),
    )
    resolver = AsyncMock(
        side_effect=OperationError(OperationErrorCode.DEADLINE_EXCEEDED, "late target")
    )
    monkeypatch.setattr(server, "_resolve_transaction_target", resolver)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())

    result = asyncio.run(
        TransactionEngine().run(
            request,
            ports=server._ForegroundTransactionRuntime(request).ports(),
        )
    )

    assert result.status.value == "outcome_unknown"
    assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
    assert result.retry_safe is False
    resolver.assert_awaited_once()


def test_post_open_captured_target_failure_is_never_retry_safe(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    observed = BrowserTarget(
        browser="Google Chrome",
        pid=73,
        title="Pull request 42",
        url=request.target.url,
    )
    monkeypatch.setattr(
        server,
        "_open_transaction_target_until_present",
        AsyncMock(
            return_value=server._TransactionOpenResult(
                action_dispatched=True,
                opened=True,
                observed_targets=(observed,),
                resolution_query=request.target.query,
            )
        ),
    )
    resolver = AsyncMock(
        side_effect=OperationError(OperationErrorCode.DEADLINE_EXCEEDED, "late target")
    )
    monkeypatch.setattr(server, "_resolve_transaction_target", resolver)
    monkeypatch.setattr(server, "_get_transaction_target_resolver", MagicMock())
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())

    result = asyncio.run(
        TransactionEngine().run(
            request,
            ports=server._ForegroundTransactionRuntime(request).ports(),
        )
    )

    assert result.status.value == "outcome_unknown"
    assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
    assert result.retry_safe is False
    resolver.assert_awaited_once()


def test_open_gate_uses_one_strict_scan_then_monitors_without_replay(
    monkeypatch,
) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    appeared = BrowserTarget(
        browser="Google Chrome",
        pid=73,
        title="Pull request 42",
        url=request.target.url,
    )
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    scans: list[dict[str, object]] = []

    def inventory(_adapter, **kwargs):
        scans.append(kwargs)
        return [] if kwargs.get("require_complete") else [appeared]

    opener = MagicMock(return_value=(True, "opened"))

    async def event_runner(pids, action, condition, **kwargs):
        assert pids == (73,)
        assert kwargs["require_all_subscriptions"] is True
        assert kwargs["abort_dispatch_on_change"] is True
        assert condition() is False
        kwargs["pre_dispatch_check"]()
        action_result = action()
        assert condition() is True
        return SimpleNamespace(
            action_result=action_result,
            action_dispatched=True,
            condition_met=True,
        )

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", inventory)
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    result = asyncio.run(
        server._open_transaction_target_until_present(
            request.target,
            budget=OperationBudget.start(1.0),
        )
    )

    assert result.action_dispatched is True
    assert result.opened is True
    assert result.observed_targets == (appeared,)
    assert sum(bool(scan.get("require_complete")) for scan in scans) == 1
    assert scans[0]["tree_depth"] == 10
    assert scans[0]["apps"] == [app]
    opener.assert_called_once_with(request.target.url)


def test_open_gate_reuses_present_target_without_dispatch(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    existing = BrowserTarget(
        browser="Google Chrome",
        pid=73,
        title="Pull request 42",
        url="",
    )
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    opener = MagicMock(side_effect=AssertionError("URL must not be opened"))

    async def event_runner(_pids, _action, condition, **_kwargs):
        assert condition() is True
        return SimpleNamespace(
            action_result=None,
            action_dispatched=False,
            condition_met=True,
        )

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(
        server,
        "collect_browser_targets",
        lambda _adapter, **_kwargs: [existing],
    )
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    result = asyncio.run(
        server._open_transaction_target_until_present(
            request.target,
            budget=OperationBudget.start(1.0),
        )
    )

    assert result.action_dispatched is False
    assert result.opened is False
    assert result.observed_targets == (existing,)
    assert result.resolution_query == request.target.query
    opener.assert_not_called()


def test_open_gate_treats_hidden_url_evidence_as_unknown(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    unknown = BrowserTarget(
        browser="Google Chrome",
        pid=73,
        title="Unrelated tab",
        url="",
    )
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    opener = MagicMock(side_effect=AssertionError("URL must not be opened"))

    async def event_runner(_pids, _action, condition, **_kwargs):
        condition()
        raise AssertionError("unknown evidence must abort inside the condition")

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(
        server,
        "collect_browser_targets",
        lambda _adapter, **_kwargs: [unknown],
    )
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(OperationError) as exc_info:
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )

    assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
    opener.assert_not_called()


def test_open_gate_aborts_when_browser_process_cohort_changes(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    chrome = AppInfo(pid=73, name="Google Chrome", windows=[])
    firefox = AppInfo(pid=74, name="Firefox", windows=[])
    adapter = MagicMock()
    adapter.list_apps_complete.side_effect = [[chrome], [chrome], [chrome, firefox]]
    opener = MagicMock(side_effect=AssertionError("URL must not be opened"))

    async def event_runner(_pids, _action, condition, **_kwargs):
        condition()
        raise AssertionError("changed cohort must abort inside the condition")

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(OperationError) as exc_info:
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )

    assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
    opener.assert_not_called()


def test_open_gate_rechecks_browser_cohort_on_the_action_worker(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    chrome = AppInfo(pid=73, name="Google Chrome", windows=[])
    firefox = AppInfo(pid=74, name="Firefox", windows=[])
    adapter = MagicMock()
    adapter.list_apps_complete.side_effect = [
        [chrome],
        [chrome],
        [chrome],
        [chrome, firefox],
    ]
    opener = MagicMock(side_effect=AssertionError("URL must not be opened"))

    async def event_runner(_pids, action, condition, **kwargs):
        assert condition() is False
        kwargs["pre_dispatch_check"]()
        action()
        raise AssertionError("changed cohort must stop before the opener")

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(OperationError) as exc_info:
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )

    assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
    opener.assert_not_called()


def test_open_gate_never_dispatches_when_strict_inventory_fails(monkeypatch) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    opener = MagicMock(side_effect=AssertionError("URL must not be opened"))

    async def event_runner(_pids, _action, condition, **_kwargs):
        condition()
        raise AssertionError("inventory failure must abort inside the condition")

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(
        server,
        "collect_browser_targets",
        MagicMock(side_effect=RuntimeError("browser inventory unavailable")),
    )
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(RuntimeError, match="inventory unavailable"):
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )
    opener.assert_not_called()


def test_malformed_opener_result_plus_post_dispatch_error_is_outcome_unknown(
    monkeypatch,
) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    opener = MagicMock(return_value=None)

    async def event_runner(_pids, action, condition, **kwargs):
        assert condition() is False
        kwargs["pre_dispatch_check"]()
        action()
        raise OperationError(OperationErrorCode.DEADLINE_EXCEEDED, "late observer")

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(server._NativeMutationOutcomeUnknown):
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )
    opener.assert_called_once_with(request.target.url)


@pytest.mark.parametrize(
    "runner_result",
    [
        None,
        SimpleNamespace(
            action_result=(True, "opened"),
            action_dispatched=False,
            condition_met=True,
        ),
    ],
)
def test_malformed_or_contradictory_observer_result_after_open_is_outcome_unknown(
    monkeypatch,
    runner_result,
) -> None:
    from agent_eyes import server

    request = _open_request()
    app = AppInfo(pid=73, name="Google Chrome", windows=[])
    adapter = MagicMock()
    adapter.list_apps_complete.return_value = [app]
    opener = MagicMock(return_value=(True, "opened"))

    async def event_runner(_pids, action, condition, **kwargs):
        assert condition() is False
        kwargs["pre_dispatch_check"]()
        action()
        return runner_result

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", _InlineWorker())
    monkeypatch.setattr(server, "collect_browser_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server._pu, "open_url_in_browser", opener)
    monkeypatch.setattr(server, "run_native_action_until_any", event_runner)

    with pytest.raises(server._NativeMutationOutcomeUnknown):
        asyncio.run(
            server._open_transaction_target_until_present(
                request.target,
                budget=OperationBudget.start(1.0),
            )
        )
    opener.assert_called_once_with(request.target.url)


def test_shared_inventory_producer_outlives_first_waiters_short_budget(
    monkeypatch,
) -> None:
    from agent_eyes import server

    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        producer_budget_remaining: list[float] = []

        class SharedWorker:
            async def run(self, call, *, budget, operation):
                assert operation == "transaction browser inventory"
                producer_budget_remaining.append(budget.remaining())
                started.set()
                await release.wait()
                return call()

        window = UIElement(
            id=1,
            role="window",
            name="Pull request 42",
            platform_ref=object(),
            children=[
                UIElement(
                    id=2,
                    role="tab",
                    name="Pull request 42",
                    platform_ref=object(),
                )
            ],
        )

        class Adapter:
            def __init__(self) -> None:
                self.list_apps_calls = 0

            def list_apps(self):
                self.list_apps_calls += 1
                return [AppInfo(pid=73, name="Firefox", windows=["Pull request 42"])]

            def get_browser_trees(self, pid: int, max_depth: int = 8):
                assert pid == 73
                assert max_depth == 8
                return [window]

        adapter = Adapter()
        worker = SharedWorker()
        monkeypatch.setattr(server, "native_worker", worker)
        monkeypatch.setattr(server, "native_adapter", adapter)
        monkeypatch.setattr(server, "_transaction_target_resolver", None)
        spec = TargetSpec(mode=TargetMode.FOREGROUND, query="Pull request 42")

        short = asyncio.create_task(
            server._resolve_transaction_target(
                spec,
                activate=False,
                budget=OperationBudget.start(0.01),
            )
        )
        await started.wait()
        long = asyncio.create_task(
            server._resolve_transaction_target(
                spec,
                activate=False,
                budget=OperationBudget.start(0.5),
            )
        )

        with pytest.raises(OperationError) as exc_info:
            await short
        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        release.set()
        resolution = await long

        assert resolution.target.pid == 73
        assert producer_budget_remaining[0] > 4.0
        assert adapter.list_apps_calls == 1

    asyncio.run(run())


def test_execute_public_call_routes_exact_shadow_target_to_persistent_cdp(
    monkeypatch,
) -> None:
    from agent_eyes import server

    class Session:
        target_id = "shadow-target-1"
        generation = 7

        async def enable_domain(self, _domain: str) -> None:
            return None

        async def send(
            self,
            method: str,
            _params: dict | None = None,
            *,
            idempotent: bool = False,
        ) -> dict:
            if method == "Page.getFrameTree":
                assert idempotent is True
                return {
                    "frameTree": {"frame": {"id": "frame-1", "loaderId": "loader-1"}}
                }
            if method == "DOM.getDocument":
                assert idempotent is True
                return {"root": {"backendNodeId": 1}}
            if method == "Accessibility.getFullAXTree":
                assert idempotent is True
                return {
                    "nodes": [
                        {
                            "nodeId": "root",
                            "ignored": False,
                            "role": {"value": "RootWebArea"},
                            "name": {"value": "Pull request"},
                            "childIds": ["button"],
                            "properties": [],
                        },
                        {
                            "nodeId": "button",
                            "ignored": False,
                            "role": {"value": "button"},
                            "name": {"value": "Add comment"},
                            "backendDOMNodeId": 101,
                            "childIds": [],
                            "properties": [],
                        },
                    ]
                }
            raise AssertionError(f"unexpected persistent CDP method: {method}")

    session = Session()
    tab = SimpleNamespace(
        id="shadow-target-1",
        title="Pull request",
        url="https://example.test/pr/1",
    )
    ensure_connected = AsyncMock(return_value=None)
    legacy_probe = AsyncMock(
        side_effect=AssertionError("persistent transaction must not probe legacy CDP")
    )
    readiness = AsyncMock(
        side_effect=AssertionError("shadow must not probe native readiness")
    )
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", ensure_connected)
    monkeypatch.setattr(server.cdp_pool, "list_tabs", lambda: [tab])
    monkeypatch.setattr(
        server.cdp_pool,
        "get_session_for_target",
        lambda target_id: session if target_id == session.target_id else None,
    )
    monkeypatch.setattr(server.cdp_client, "is_available", legacy_probe)
    monkeypatch.setattr(
        server.cdp_client,
        "collect_secure_dom_metadata",
        AsyncMock(return_value=SecureDOMMetadata(complete=True)),
    )
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)
    monkeypatch.setattr(server, "_transaction_target_resolver", None)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())

    result = asyncio.run(
        server.call_tool(
            "execute",
            {
                "target": {
                    "target_id": "shadow-target-1",
                    "mode": "shadow",
                },
                "steps": [
                    {
                        "op": "expect",
                        "role": "button",
                        "name": "Add comment",
                    }
                ],
            },
        )
    )

    assert isinstance(result, list)
    payload = json.loads(result[0].text)
    assert payload["status"] == "succeeded"
    assert payload["target_id"] == "shadow-target-1"
    assert payload["completed_steps"] == 1
    assert payload["snapshot"].startswith("n")
    ensure_connected.assert_awaited_once()
    legacy_probe.assert_not_awaited()
    readiness.assert_not_awaited()


def test_transaction_mutation_invalidates_exact_browser_and_pid_snapshots(
    monkeypatch,
) -> None:
    from agent_eyes import server

    local_coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "coordinator", local_coordinator)
    exact = local_coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="native:browser:73:window:1:tab:2",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=1, value=object())],
    )
    pid_snapshot = local_coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:73",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=2, value=object())],
    )

    server._invalidate_native_mutation_state(
        pid=73,
        target_id="native:browser:73:window:1:tab:2",
    )

    with pytest.raises(OperationError, match="STALE_SNAPSHOT"):
        local_coordinator.observations.get_snapshot(exact.token)
    with pytest.raises(OperationError, match="STALE_SNAPSHOT"):
        local_coordinator.observations.get_snapshot(pid_snapshot.token)
