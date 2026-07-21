from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.cdp import CDPDocumentChangedError, CDPMutationOutcomeUnknown
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.observations import ElementRecord
from agent_eyes.operation import OperationBudget, OperationMode
from agent_eyes.operation import OperationError, OperationErrorCode
from agent_eyes.provider_worker import ProviderWorker


def _snapshot_token(output: str) -> str:
    header = output.splitlines()[0]
    assert header.startswith("snapshot=")
    return header.removeprefix("snapshot=")


def _native_tree(label: str) -> UIElement:
    return UIElement(
        id=1,
        role="window",
        name=label,
        children=[UIElement(id=2, role="button", name=f"{label} button")],
    )


def test_focused_element_emits_actionable_native_snapshot(monkeypatch):
    from agent_eyes import server

    focused = UIElement(id=41, role="textfield", name="Search", pid=73)
    adapter = MagicMock()
    adapter.get_focused_element.return_value = focused
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", coordinator)

    output = asyncio.run(server._handle_get_focused_async())
    token = _snapshot_token(output)
    snapshot, record = coordinator.observations.resolve_with_snapshot(token, 41)

    assert snapshot.provider == "native"
    assert snapshot.mode is OperationMode.FOREGROUND
    assert snapshot.target_id == "pid:73"
    assert record.value is focused


def test_subtree_requires_and_derives_from_exact_native_snapshot(monkeypatch):
    from agent_eyes import server

    root = UIElement(id=9, role="group", name="Settings", pid=73, platform_ref=object())
    child = UIElement(id=10, role="button", name="Save", pid=73)
    expanded = UIElement(id=9, role="group", name="Settings", children=[child], pid=73)
    coordinator = AutomationCoordinator()
    source = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:73",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=9, value=root)],
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.get_subtree.return_value = expanded
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", coordinator)

    missing = asyncio.run(server._handle_get_subtree_async({"id": 9}))
    output = asyncio.run(
        server._handle_get_subtree_async(
            {"snapshot": source.token, "id": 9, "max_depth": 3}
        )
    )
    token = _snapshot_token(output)
    derived, record = coordinator.observations.resolve_with_snapshot(token, 10)

    assert "snapshot is required" in missing
    assert derived.provider == "native"
    assert derived.target_id == "pid:73"
    assert record.value is child
    adapter.get_subtree.assert_called_once_with(root, max_depth=3)


def test_context_emits_snapshot_when_it_displays_focused_id(monkeypatch):
    from agent_eyes import server

    focused = UIElement(id=12, role="button", name="Publish", pid=73)
    adapter = MagicMock()
    adapter.list_apps.return_value = [
        SimpleNamespace(
            pid=73,
            name="Browser",
            windows=["Article"],
            is_frontmost=True,
        )
    ]
    adapter.get_focused_element.return_value = focused
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", coordinator)

    output = asyncio.run(server._handle_context_async({}))
    token = _snapshot_token(output)
    snapshot, record = coordinator.observations.resolve_with_snapshot(token, 12)

    assert "Browser — Article | [12] button \"Publish\" focused" in output
    assert snapshot.target_id == "pid:73"
    assert record.value is focused


def test_tree_emits_snapshot_and_later_tree_cannot_redirect_qualified_click(monkeypatch):
    from agent_eyes import server

    trees = {
        101: _native_tree("first"),
        202: _native_tree("second"),
    }
    actions: list[str] = []
    adapter = MagicMock()
    adapter.get_tree.side_effect = lambda pid, *args, **kwargs: trees[pid]
    adapter.is_element_valid.return_value = True
    adapter.perform_action.side_effect = lambda element, action: (
        actions.append(element.name) or True
    )
    backend = MagicMock()
    backend.is_available.return_value = False
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    first_output = asyncio.run(server._handle_get_tree({"pid": 101}))
    second_output = asyncio.run(server._handle_get_tree({"pid": 202}))
    first_snapshot = _snapshot_token(first_output)
    second_snapshot = _snapshot_token(second_output)

    assert first_snapshot != second_snapshot
    result = asyncio.run(server._handle_click({"snapshot": first_snapshot, "id": 2}))

    assert result == 'clicked [2] button "first button"'
    assert actions == ["first button"]


def test_bare_id_fails_closed_when_multiple_live_snapshots_are_ambiguous(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    adapter.get_tree.side_effect = lambda pid, *args, **kwargs: _native_tree(str(pid))
    adapter.perform_action.return_value = True
    backend = MagicMock()
    backend.is_available.return_value = False
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    asyncio.run(server._handle_get_tree({"pid": 1}))
    asyncio.run(server._handle_get_tree({"pid": 2}))
    result = asyncio.run(server._handle_click({"id": 2}))

    assert "AMBIGUOUS_TARGET" in result
    adapter.perform_action.assert_not_called()


def test_generic_click_rejects_shadow_snapshot_without_explicit_shadow(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="remote",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=1,
        revision=1,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    shadow_probe = AsyncMock(side_effect=AssertionError("shadow provider was probed"))
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_ensure_tabs", shadow_probe)

    result = asyncio.run(server._handle_click({"snapshot": snapshot.token, "id": 9}))

    assert "MODE_MISMATCH" in result
    shadow_probe.assert_not_awaited()


def test_qualified_shadow_action_uses_canonical_target_as_lock_key(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="remote",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=4,
        revision=8,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    monkeypatch.setattr(server, "coordinator", coordinator)

    resolved, metadata, mode, action_key, error = server._resolve_action_element(
        {"snapshot": snapshot.token, "id": 9, "shadow": True},
        "click",
    )

    assert error == ""
    assert resolved is element
    assert metadata is snapshot
    assert mode is OperationMode.SHADOW
    assert action_key == "target-1"


def test_shadow_web_tree_emits_target_bound_snapshot(monkeypatch):
    from agent_eyes import server

    session = SimpleNamespace(
        target_id="target-7",
        generation=4,
        enable_domain=AsyncMock(),
        send=AsyncMock(return_value={"nodes": [{"nodeId": "root"}]}),
    )
    tab = SimpleNamespace(
        id="target-7",
        title="Example",
        url="https://example.test",
    )
    tree = UIElement(
        id=1,
        role="RootWebArea",
        children=[
            UIElement(
                id=42,
                role="button",
                name="Submit",
                source="cdp",
                platform_ref=700,
            )
        ],
    )
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(
        server.cdp_client,
        "_build_tree",
        lambda _nodes, **_kwargs: tree,
    )
    monkeypatch.setattr(server, "_get_chrome_pid", lambda: 0)
    monkeypatch.setattr(
        server,
        "_append_persistent_shadow_dom_elements",
        AsyncMock(return_value=0),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=101),
        raising=False,
    )

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": "target-7"}
        )
    )
    token = _snapshot_token(output)
    metadata, record = coordinator.observations.resolve_with_snapshot(token, 42)

    assert metadata.provider == "cdp-persistent"
    assert metadata.target_id == "target-7"
    assert metadata.generation == 4
    assert metadata.revision == 101
    assert record.value is tree.children[0]
    assert record.actionable is True


def test_persistent_shadow_tree_redacts_dom_classified_secure_textbox(monkeypatch):
    from agent_eyes import server

    secret = "persistent-password-secret-c1e7"
    nodes = [
        {
            "nodeId": "root",
            "ignored": False,
            "role": {"value": "RootWebArea"},
            "childIds": ["field"],
            "properties": [],
        },
        {
            "nodeId": "field",
            "ignored": False,
            "role": {"value": "textbox"},
            "value": {"value": secret},
            "backendDOMNodeId": 701,
            "childIds": [],
            "properties": [],
        },
    ]

    async def send(method, params=None, *, idempotent=False):
        assert idempotent is True
        if method == "Accessibility.getFullAXTree":
            return {"nodes": nodes}
        if method == "DOM.getDocument":
            assert params == {"depth": 0, "pierce": False}
            return {"root": {"nodeId": 1}}
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            assert params == {"backendNodeIds": [701]}
            return {"nodeIds": [1701]}
        if method == "DOM.performSearch":
            return {"searchId": "secure-search", "resultCount": 1}
        if method == "DOM.getSearchResults":
            assert params == {
                "searchId": "secure-search",
                "fromIndex": 0,
                "toIndex": 1,
            }
            return {"nodeIds": [1701]}
        if method == "DOM.getNodesForSubtreeByStyle":
            return {"nodeIds": []}
        if method == "DOM.discardSearchResults":
            return {}
        raise AssertionError(method)

    session = SimpleNamespace(
        target_id="target-secure",
        generation=5,
        enable_domain=AsyncMock(),
        send=AsyncMock(side_effect=send),
    )
    tab = SimpleNamespace(
        id="target-secure",
        title="Sign in",
        url="https://example.test/login",
    )
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(server, "_get_chrome_pid", lambda: 0)
    monkeypatch.setattr(
        server,
        "_append_persistent_shadow_dom_elements",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=501),
    )

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": "target-secure", "full": True}
        )
    )

    token = _snapshot_token(output)
    snapshot = coordinator.observations.get_snapshot(token)
    field = next(
        record.value
        for record in snapshot.elements
        if record.value.platform_ref == 701
    )
    assert secret not in output
    assert field.value == ""
    assert "secure" in field.states


def test_apple_web_tree_is_exact_target_bound_and_read_only(monkeypatch):
    from agent_eyes import applescript, server

    target = applescript.AppleScriptTab(
        index=0,
        window_index=2,
        title="Apple target",
        url="https://example.test",
        id="tab-a",
        window_id="window-a",
    )
    tree_payload = {
        "id": 1,
        "role": "document",
        "name": "",
        "bounds": [0, 0, 100, 100],
        "interactive": False,
        "children": [
            {
                "id": 2,
                "role": "button",
                "name": "Submit",
                "bounds": [1, 2, 30, 20],
                "interactive": True,
                "children": [],
            }
        ],
    }
    apple = MagicMock()
    apple.is_available.return_value = True
    apple.list_chrome_tabs.return_value = [target]
    apple.execute_javascript.return_value = __import__("json").dumps(tree_payload)
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_as", apple)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_as_tabs_cache", [])
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(None, None, "")),
    )

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": target.identifier}
        )
    )
    token = _snapshot_token(output)
    snapshot, record = coordinator.observations.resolve_with_snapshot(token, 2)
    click = asyncio.run(
        server._handle_click(
            {"shadow": True, "snapshot": token, "id": 2}
        )
    )

    assert snapshot.provider == "apple-events"
    assert snapshot.target_id == target.identifier
    assert record.value.name == "Submit"
    assert record.actionable is False
    assert "UNSUPPORTED_CAPABILITY" in click
    apple.execute_javascript.assert_called_once_with(
        server.build_ax_tree_script(max_depth=5),
        tab_index=0,
        window_index=2,
        tab_id="tab-a",
        window_id="window-a",
    )


def test_shadow_web_tree_never_falls_back_to_cached_tab_for_unknown_target(monkeypatch):
    from agent_eyes import server

    stale_cached_tab = SimpleNamespace(
        id="different-target",
        title="Wrong tab",
        url="https://wrong.example",
    )
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(
            return_value=(None, None, "ERROR: Requested shadow target is unavailable.")
        ),
    )
    monkeypatch.setattr(server, "_cached_tabs", [stale_cached_tab])
    get_tree = AsyncMock()
    monkeypatch.setattr(server.cdp_client, "get_accessibility_tree", get_tree)

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": "missing-target", "tab_index": 0}
        )
    )

    assert output == "ERROR: Requested shadow target is unavailable."
    get_tree.assert_not_awaited()


def test_cdp_session_target_id_never_routes_by_duplicate_url(monkeypatch):
    from agent_eyes import server

    tab_a = SimpleNamespace(id="target-a", url="https://same.test")
    tab_b = SimpleNamespace(id="target-b", url="https://same.test")
    session_b = object()
    pool = MagicMock()
    pool.ensure_connected = AsyncMock()
    pool.list_tabs.return_value = [tab_a, tab_b]
    pool.get_session_for_target.return_value = session_b
    monkeypatch.setattr(server, "cdp_pool", pool)

    session, tab, error = asyncio.run(
        server._get_cdp_session({"target_id": "target-b"})
    )

    assert error == ""
    assert session is session_b
    assert tab is tab_b
    pool.get_session_for_target.assert_called_once_with("target-b")


def test_shadow_click_uses_snapshot_target_generation_and_revision(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=5,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-b",
        generation=5,
        send=AsyncMock(
            side_effect=[
                {"object": {"objectId": "object-1"}},
                {},
            ]
        ),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=77),
        raising=False,
    )

    result = asyncio.run(
        server._handle_click(
            {"snapshot": snapshot.token, "id": 9, "shadow": True}
        )
    )

    assert result == 'clicked [9] button "Submit"'
    pool.get_session_for_target.assert_called_with("target-b")
    assert session.send.await_args_list[0].args == (
        "DOM.resolveNode",
        {"backendNodeId": 123},
    )
    assert session.send.await_args_list[1].args[0] == "Runtime.callFunctionOn"


def test_persistent_shadow_click_rejects_runtime_stale_element_exception(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=5,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-b",
        generation=5,
        send=AsyncMock(
            side_effect=[
                {"object": {"objectId": "object-1"}},
                {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "Error: STALE_ELEMENT"},
                    }
                },
            ]
        ),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=77),
    )

    result = asyncio.run(
        server._handle_click({"snapshot": snapshot.token, "id": 9, "shadow": True})
    )

    assert "STALE_SNAPSHOT" in result
    assert "clicked [9]" not in result
    with pytest.raises(OperationError) as exc_info:
        coordinator.observations.resolve(snapshot.token, 9)
    assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT


def test_persistent_shadow_click_marks_unknown_runtime_exception(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=5,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-b",
        generation=5,
        send=AsyncMock(
            side_effect=[
                {"object": {"objectId": "object-1"}},
                {"exceptionDetails": {"text": "Uncaught"}},
            ]
        ),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=77),
    )

    result = asyncio.run(
        server._handle_click({"snapshot": snapshot.token, "id": 9, "shadow": True})
    )

    assert "OUTCOME_UNKNOWN" in result
    assert "clicked [9]" not in result


def test_shadow_click_rechecks_document_after_resolving_before_dispatch(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=5,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-b",
        generation=5,
        send=AsyncMock(return_value={"object": {"objectId": "object-1"}}),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(side_effect=[77, 78]),
    )

    result = asyncio.run(
        server._handle_click(
            {"snapshot": snapshot.token, "id": 9, "shadow": True}
        )
    )

    assert "STALE_SNAPSHOT" in result
    assert session.send.await_count == 1
    assert session.send.await_args.args[0] == "DOM.resolveNode"


def test_persistent_shadow_type_never_inserts_text_after_focus_exception(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=801,
        role="textbox",
        name="Search",
        source="cdp",
        platform_ref=99,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=1,
        revision=1,
        elements=[ElementRecord(local_id=801, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-1",
        generation=1,
        send=AsyncMock(
            side_effect=[
                {"object": {"objectId": "object-1"}},
                {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "Error: STALE_ELEMENT"},
                    }
                },
            ]
        ),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=1),
    )

    result = asyncio.run(
        server._handle_type(
            {
                "id": 801,
                "text": "secret",
                "snapshot": snapshot.token,
                "shadow": True,
            }
        )
    )

    assert "STALE_SNAPSHOT" in result
    assert "text was not sent" in result
    assert [call.args[0] for call in session.send.await_args_list] == [
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
    ]


def test_persistent_shadow_type_reports_verification_exception_as_warning(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=801,
        role="textbox",
        name="Search",
        source="cdp",
        platform_ref=99,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-1",
        generation=1,
        revision=1,
        elements=[ElementRecord(local_id=801, value=element)],
    )
    session = SimpleNamespace(
        target_id="target-1",
        generation=1,
        send=AsyncMock(
            side_effect=[
                {"object": {"objectId": "object-1"}},
                {},
                {},
                {"exceptionDetails": {"text": "Uncaught"}},
            ]
        ),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=1),
    )

    result = asyncio.run(
        server._handle_type(
            {
                "id": 801,
                "text": "hello",
                "snapshot": snapshot.token,
                "shadow": True,
            }
        )
    )

    assert result.startswith("WARNING: dispatched 5 characters")
    assert [call.args[0] for call in session.send.await_args_list] == [
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "Input.insertText",
        "Runtime.callFunctionOn",
    ]


def test_persistent_document_revision_includes_dom_root_identity():
    from agent_eyes import server

    async def revision(root_backend_id: int) -> int:
        session = SimpleNamespace(
            send=AsyncMock(
                side_effect=[
                    {
                        "frameTree": {
                            "frame": {"id": "frame-1", "loaderId": "loader-1"}
                        }
                    },
                    {"root": {"backendNodeId": root_backend_id}},
                ]
            )
        )
        return await server._persistent_document_revision(session)

    first = asyncio.run(revision(100))
    replaced = asyncio.run(revision(200))

    assert first != replaced


def test_shadow_web_tree_retries_one_document_race_before_snapshot(monkeypatch):
    from agent_eyes import server

    session = SimpleNamespace(
        target_id="target-race",
        generation=2,
        enable_domain=AsyncMock(),
        send=AsyncMock(return_value={"nodes": [{"nodeId": "root"}]}),
    )
    tab = SimpleNamespace(
        id="target-race",
        title="Example",
        url="https://example.test",
    )
    build_calls = 0

    def build_tree(_nodes, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        return UIElement(
            id=build_calls,
            role="button",
            name="Submit",
            source="cdp",
            platform_ref=700 + build_calls,
        )

    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(server.cdp_client, "_build_tree", build_tree)
    monkeypatch.setattr(server, "_get_chrome_pid", lambda: 0)
    monkeypatch.setattr(
        server,
        "_append_persistent_shadow_dom_elements",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(side_effect=[10, 11, 12, 12]),
    )

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": "target-race"}
        )
    )
    token = _snapshot_token(output)
    metadata, _record = coordinator.observations.resolve_with_snapshot(token, 2)

    assert metadata.revision == 12
    assert build_calls == 2


def test_legacy_shadow_web_tree_brackets_tree_with_document_revision(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    tab = SimpleNamespace(
        id="legacy-target",
        title="Legacy",
        url="https://example.test",
    )
    tree = UIElement(
        id=1,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=77,
    )
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(None, tab, "")),
    )
    revision = AsyncMock(side_effect=[81, 81])
    monkeypatch.setattr(server.cdp_client, "get_document_revision", revision)
    monkeypatch.setattr(
        server.cdp_client,
        "get_accessibility_tree",
        AsyncMock(return_value=tree),
    )
    monkeypatch.setattr(server, "_get_chrome_pid", lambda: 0)
    monkeypatch.setattr(
        server,
        "_append_shadow_dom_elements",
        AsyncMock(return_value=0),
    )

    output = asyncio.run(
        server._handle_get_web_tree(
            {"shadow": True, "target_id": "legacy-target"}
        )
    )
    token = _snapshot_token(output)
    snapshot, _record = coordinator.observations.resolve_with_snapshot(token, 1)

    assert snapshot.provider == "cdp-legacy"
    assert snapshot.target_id == "legacy-target"
    assert snapshot.revision == 81
    assert revision.await_count == 2


def test_legacy_shadow_click_passes_snapshot_revision_and_reports_unknown(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-legacy",
        mode=OperationMode.SHADOW,
        target_id="legacy-target",
        generation=0,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    tab = SimpleNamespace(id="legacy-target", url="https://example.test")
    click = AsyncMock(
        side_effect=CDPMutationOutcomeUnknown("response was not confirmed")
    )
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_cached_tabs", [tab])
    monkeypatch.setattr(server, "_ensure_tabs", AsyncMock(return_value=""))
    monkeypatch.setattr(server.cdp_client, "click_element", click)

    output = asyncio.run(
        server._handle_click(
            {"shadow": True, "snapshot": snapshot.token, "id": 9}
        )
    )

    assert "OUTCOME_UNKNOWN" in output
    click.assert_awaited_once_with(tab, 123, expected_revision=77)


def test_legacy_shadow_click_reports_stale_document_without_dispatch(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    element = UIElement(
        id=9,
        role="button",
        name="Submit",
        source="cdp",
        platform_ref=123,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-legacy",
        mode=OperationMode.SHADOW,
        target_id="legacy-target",
        generation=0,
        revision=77,
        elements=[ElementRecord(local_id=9, value=element)],
    )
    tab = SimpleNamespace(id="legacy-target", url="https://example.test")
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_cached_tabs", [tab])
    monkeypatch.setattr(server, "_ensure_tabs", AsyncMock(return_value=""))
    monkeypatch.setattr(
        server.cdp_client,
        "click_element",
        AsyncMock(side_effect=CDPDocumentChangedError("document changed")),
    )

    output = asyncio.run(
        server._handle_click(
            {"shadow": True, "snapshot": snapshot.token, "id": 9}
        )
    )

    assert "STALE_SNAPSHOT" in output


def test_upload_uses_snapshot_target_and_accepts_shadow_dom_node(monkeypatch, tmp_path):
    from agent_eyes import server

    upload_file = tmp_path / "upload.txt"
    upload_file.write_text("safe", encoding="utf-8")
    coordinator = AutomationCoordinator()
    element = UIElement(
        id=90,
        role="textbox",
        name="File",
        source="shadow-dom",
        platform_ref=909,
    )
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=6,
        revision=44,
        elements=[ElementRecord(local_id=90, value=element)],
    )
    session = SimpleNamespace(
        generation=6,
        set_file_input=AsyncMock(),
    )
    pool = MagicMock()
    pool.get_session_for_target.return_value = session
    legacy = AsyncMock(side_effect=AssertionError("legacy target was mutated"))
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "cdp_pool", pool)
    monkeypatch.setattr(server.cdp_client, "set_file_input", legacy)
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(return_value=44),
    )

    result = asyncio.run(
        server._handle_file_upload(
            {
                "snapshot": snapshot.token,
                "id": 90,
                "files": [str(upload_file)],
                "shadow": True,
            }
        )
    )

    assert result == "Uploaded 1 file(s) to [90]."
    session.set_file_input.assert_awaited_once_with(909, [str(upload_file)])
    legacy.assert_not_awaited()


def test_upload_path_validation_rejects_protected_aliases_and_allows_siblings(tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    exact_dir = home / ".ssh"
    exact_dir.mkdir(parents=True)
    exact_key = exact_dir / "id_ed25519"
    exact_key.write_text("secret", encoding="utf-8")

    case_alias_dir = home / ".SSH"
    case_alias_dir.mkdir(exist_ok=True)
    case_alias_key = case_alias_dir / "case-key"
    case_alias_key.write_text("secret", encoding="utf-8")

    sibling_dir = home / ".ssh-backup"
    sibling_dir.mkdir()
    sibling_file = sibling_dir / "public.txt"
    sibling_file.write_text("safe", encoding="utf-8")

    assert server._validate_upload_paths([str(exact_key)], home=home)[1] == "protected"
    assert (
        server._validate_upload_paths([str(case_alias_key)], home=home)[1]
        == "protected"
    )
    assert server._validate_upload_paths([str(sibling_file)], home=home) == (
        [str(sibling_file.resolve())],
        "",
    )


def test_upload_path_validation_resolves_protected_directory_symlinks(tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    credential = vault / "credentials"
    credential.write_text("secret", encoding="utf-8")
    (home / ".aws").symlink_to(vault, target_is_directory=True)

    outside_alias = tmp_path / "outside-credential"
    outside_alias.symlink_to(credential)

    assert (
        server._validate_upload_paths([str(home / ".aws" / "credentials")], home=home)[
            1
        ]
        == "protected"
    )
    assert (
        server._validate_upload_paths([str(outside_alias)], home=home)[1]
        == "protected"
    )


def test_upload_path_validation_rejects_hard_link_to_protected_file(tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    home.mkdir()
    protected = home / ".netrc"
    protected.write_text("machine example.test", encoding="utf-8")
    outside_hard_link = tmp_path / "outside-netrc"
    try:
        os.link(protected, outside_hard_link)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {type(exc).__name__}")

    assert (
        server._validate_upload_paths([str(outside_hard_link)], home=home)[1]
        == "protected"
    )


def test_upload_path_validation_rejects_non_regular_and_missing_paths(tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    home.mkdir()
    directory = tmp_path / "directory"
    directory.mkdir()

    assert server._validate_upload_paths([str(directory)], home=home)[1] == "missing"
    assert (
        server._validate_upload_paths([str(tmp_path / "missing")], home=home)[1]
        == "missing"
    )

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        assert server._validate_upload_paths([str(fifo)], home=home)[1] == "missing"


@pytest.mark.skipif(not Path("/etc/hosts").is_file(), reason="no /etc/hosts")
def test_upload_path_validation_rejects_canonicalized_etc_file(tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    home.mkdir()

    assert server._validate_upload_paths(["/etc/hosts"], home=home)[1] == "protected"


def test_upload_path_comparison_errors_fail_closed(monkeypatch, tmp_path):
    from agent_eyes import server

    home = tmp_path / "home"
    protected_dir = home / ".ssh"
    protected_dir.mkdir(parents=True)
    safe_file = tmp_path / "safe.txt"
    safe_file.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(os.path, "samefile", MagicMock(side_effect=OSError("denied")))

    assert server._validate_upload_paths([str(safe_file)], home=home)[1] == "protected"


def test_pierce_scopes_selector_and_returns_actionable_snapshot(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    session = SimpleNamespace(
        generation=3,
        pierce_selector=AsyncMock(
            return_value=[
                {
                    "nodeId": 11,
                    "backendNodeId": 211,
                    "nodeType": 1,
                    "nodeName": "BUTTON",
                    "attributes": ["aria-label", "Inside"],
                }
            ]
        ),
    )
    tab = SimpleNamespace(id="target-shadow")
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(
        server,
        "_get_cdp_session",
        AsyncMock(return_value=(session, tab, "")),
    )
    monkeypatch.setattr(
        server,
        "_persistent_document_revision",
        AsyncMock(side_effect=[9, 9]),
    )

    output = asyncio.run(
        server._handle_pierce(
            {
                "selector": "custom-shell",
                "shadow": True,
                "target_id": "target-shadow",
            }
        )
    )
    token = _snapshot_token(output)
    element_id = int(output.splitlines()[1].split("]", 1)[0].removeprefix("["))
    metadata, record = coordinator.observations.resolve_with_snapshot(
        token,
        element_id,
    )

    session.pierce_selector.assert_awaited_once_with("custom-shell")
    assert metadata.target_id == "target-shadow"
    assert record.actionable is True
    assert record.value.platform_ref == 211


def test_tree_never_displays_ids_beyond_snapshot_element_cap(monkeypatch):
    from agent_eyes import server

    tree = UIElement(
        id=0,
        role="window",
        children=[
            UIElement(id=index, role="button", name=f"Button {index}")
            for index in range(1, 502)
        ],
    )
    adapter = MagicMock()
    adapter.get_tree.return_value = tree
    coordinator = AutomationCoordinator()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda _pid: False)

    output = asyncio.run(
        server._handle_get_tree(
            {"pid": 77, "full": True, "max_depth": 2}
        )
    )
    token = _snapshot_token(output)

    assert "[499]" in output
    assert "[500]" not in output
    assert "[501]" not in output
    assert coordinator.observations.resolve(token, 499).value.id == 499
    with pytest.raises(OperationError) as exc_info:
        coordinator.observations.resolve(token, 500)
    assert exc_info.value.code is OperationErrorCode.ELEMENT_NOT_FOUND


def test_fill_form_resolves_every_field_from_one_snapshot(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    elements = [
        UIElement(
            id=index,
            role="textbox",
            name=f"Field {index}",
            source="cdp",
            platform_ref=100 + index,
        )
        for index in (1, 2)
    ]
    snapshot = coordinator.observations.create(
        provider="cdp-persistent",
        mode=OperationMode.SHADOW,
        target_id="target-b",
        generation=2,
        revision=3,
        elements=[
            ElementRecord(local_id=element.id, value=element)
            for element in elements
        ],
    )
    typer = AsyncMock(side_effect=["typed 1 characters into [1]", "typed 1 characters into [2]"])
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_handle_type_resolved", typer)

    result = asyncio.run(
        server._handle_fill_form(
            {
                "snapshot": snapshot.token,
                "shadow": True,
                "fields": [
                    {"id": 1, "value": "a"},
                    {"id": 2, "value": "b"},
                ],
            }
        )
    )

    assert result == "filled 2 field(s); 0 failed"
    assert [call.args[:3] for call in typer.await_args_list] == [
        (1, "a", elements[0]),
        (2, "b", elements[1]),
    ]
    assert all(call.kwargs["snapshot"] is snapshot for call in typer.await_args_list)


def test_fill_form_partial_failure_is_reported_as_error(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    elements = [
        UIElement(id=index, role="textbox", source="native", platform_ref=object())
        for index in (1, 2)
    ]
    snapshot = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:42",
        generation=0,
        revision=1,
        elements=[
            ElementRecord(local_id=element.id, value=element)
            for element in elements
        ],
    )
    typer = AsyncMock(
        side_effect=[
            "typed 1 characters into [1]",
            "ERROR: type [2]: FOCUS_MISMATCH: text was not sent",
        ]
    )
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_handle_type_resolved", typer)

    result = asyncio.run(
        server._handle_fill_form(
            {
                "snapshot": snapshot.token,
                "fields": [
                    {"id": 1, "value": "a"},
                    {"id": 2, "value": "b"},
                ],
            }
        )
    )

    assert result == "ERROR: PARTIAL_FAILURE: filled 1 field(s); 1 failed"


def test_click_and_type_schemas_expose_snapshot_and_explicit_shadow_mode():
    from agent_eyes.server import TOOLS

    for name in ("click", "type"):
        tool = next(candidate for candidate in TOOLS if candidate.name == name)
        properties = tool.inputSchema["properties"]

        assert properties["snapshot"]["type"] == "string"
        assert properties["shadow"]["type"] == "boolean"
        assert properties["shadow"]["default"] is False


def test_call_tool_applies_default_output_ceiling(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_dispatch", AsyncMock(return_value="x" * 1_000_000))

    result = asyncio.run(server.call_tool("status", {}))
    text = result[0].text

    assert len(text.encode("utf-8")) <= 16 * 1024
    assert "truncated" in text


def test_parallel_identical_tree_observations_call_native_provider_once(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    adapter.get_tree.return_value = _native_tree("shared")
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    async def run() -> list[str]:
        return await asyncio.gather(
            *(server._handle_get_tree({"pid": 77}) for _ in range(32))
        )

    outputs = asyncio.run(run())

    assert adapter.get_tree.call_count == 1
    assert len({_snapshot_token(output) for output in outputs}) == 32


def test_short_tree_waiter_does_not_poison_concurrent_long_waiter(monkeypatch):
    from agent_eyes import server

    started = threading.Event()
    worker = ProviderWorker("tree-deadline-isolation")
    adapter = MagicMock()

    def delayed_tree(*_args, **_kwargs):
        started.set()
        time.sleep(0.05)
        return _native_tree("shared")

    adapter.get_tree.side_effect = delayed_tree
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", worker)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda _pid: False)

    async def run() -> None:
        try:
            short = asyncio.create_task(
                server._handle_get_tree({"pid": 77, "timeout": 0.01})
            )
            while not started.is_set():
                await asyncio.sleep(0)
            long = asyncio.create_task(
                server._handle_get_tree({"pid": 77, "timeout": 1.0})
            )

            with pytest.raises(OperationError) as exc_info:
                await short
            assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
            output = await long

            assert output.startswith("snapshot=")
            assert adapter.get_tree.call_count == 1
        finally:
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_tree_total_deadline_bounds_slow_native_provider(monkeypatch):
    from agent_eyes import server

    started = threading.Event()
    release = threading.Event()
    worker = ProviderWorker("deadline-test")
    coordinator = AutomationCoordinator()
    adapter = MagicMock()

    def slow_tree(*args, **kwargs):
        started.set()
        release.wait(timeout=1.0)
        return _native_tree("late")

    adapter.get_tree.side_effect = slow_tree
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", worker)
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    async def run() -> None:
        await worker.run(
            lambda: None,
            budget=OperationBudget.start(1.0),
            operation="tree deadline worker warmup",
        )
        before = time.monotonic()
        with pytest.raises(OperationError) as exc_info:
            await server._handle_get_tree({"pid": 42, "timeout": 0.1})
        elapsed = time.monotonic() - before

        assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert elapsed < 0.2
        assert started.is_set()
        assert worker.busy is True
        assert coordinator._flights == {}

        async def unrelated_foreground_read() -> str:
            return "available"

        assert await asyncio.wait_for(
            coordinator.execute_foreground(unrelated_foreground_read),
            timeout=0.05,
        ) == "available"

        release.set()
        await worker.wait_until_idle()
        await worker.aclose()

    asyncio.run(run())


def test_late_foreground_mutation_is_unknown_invalidates_state_and_never_replays(
    monkeypatch,
):
    from agent_eyes import server

    worker = ProviderWorker("late-press-test")
    coordinator = AutomationCoordinator()
    stale = coordinator.observations.create(
        provider="native",
        mode=OperationMode.FOREGROUND,
        target_id="pid:42",
        generation=0,
        revision=1,
        elements=[ElementRecord(local_id=1, value="stale")],
    )
    started = threading.Event()
    release = threading.Event()
    applied = threading.Event()
    calls = 0

    def late_press(_key: str) -> bool:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2.0)
        applied.set()
        return True

    backend = MagicMock()
    backend.is_available.return_value = True
    backend.press_key.side_effect = late_press
    monkeypatch.setattr(server, "input_worker", worker)
    monkeypatch.setattr(server, "coordinator", coordinator)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "_native_target_cache", {"cached": object()})

    async def run() -> None:
        try:
            await worker.run(
                lambda: None,
                budget=OperationBudget.start(1.0),
                operation="late press worker warmup",
            )
            before = time.monotonic()
            output = await server._handle_press_key(
                {
                    "key": "Enter",
                    "_operation_budget": OperationBudget.start(0.1),
                }
            )
            elapsed = time.monotonic() - before

            assert "OUTCOME_UNKNOWN" in output
            assert elapsed < 0.25
            assert started.is_set()
            assert calls == 1
            assert worker.busy is True
            assert server._native_target_cache == {}
            with pytest.raises(OperationError) as exc_info:
                coordinator.observations.resolve(stale.token, 1)
            assert exc_info.value.code is OperationErrorCode.STALE_SNAPSHOT

            release.set()
            await worker.wait_until_idle()
            assert applied.is_set()
            assert calls == 1
        finally:
            release.set()
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_pre_submit_native_mutation_deadline_remains_definite(monkeypatch):
    from agent_eyes import server

    worker = ProviderWorker("queued-mutation-test")
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    mutation_applied = threading.Event()

    def blocking_query() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=2.0)

    monkeypatch.setattr(server, "native_worker", worker)

    async def run() -> None:
        blocker = asyncio.create_task(
            worker.run(
                blocking_query,
                budget=OperationBudget.start(1.0),
                operation="blocking query",
            )
        )
        while not blocker_started.is_set():
            await asyncio.sleep(0)
        try:
            with pytest.raises(OperationError) as exc_info:
                await server._run_native_mutation(
                    lambda: mutation_applied.set(),
                    budget=OperationBudget.start(0.02),
                    operation="queued foreground mutation",
                    worker=worker,
                )
            assert exc_info.value.code is OperationErrorCode.DEADLINE_EXCEEDED
            assert mutation_applied.is_set() is False
        finally:
            release_blocker.set()
            await blocker
            await worker.aclose()

    asyncio.run(run())


def test_cancelled_started_native_mutation_becomes_unknown_without_replay(monkeypatch):
    from agent_eyes import server

    worker = ProviderWorker("cancelled-mutation-test")
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_mutation() -> None:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(server, "native_worker", worker)

    async def run() -> None:
        task = asyncio.create_task(
            server._run_native_mutation(
                blocking_mutation,
                budget=OperationBudget.start(1.0),
                operation="cancelled foreground mutation",
                worker=worker,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        try:
            task.cancel()
            with pytest.raises(server._NativeMutationOutcomeUnknown):
                await task
            assert calls == 1
            assert worker.busy is True
        finally:
            release.set()
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_uncertain_mutation_poisons_other_foreground_workers_until_settled(monkeypatch):
    from agent_eyes import server

    coordinator = AutomationCoordinator()
    uncertain_worker = ProviderWorker("uncertain-global-foreground")
    other_worker = ProviderWorker("other-global-foreground")
    started = threading.Event()
    release = threading.Event()
    other_ran = False

    def blocking_mutation() -> None:
        started.set()
        release.wait(timeout=2.0)

    async def run() -> None:
        nonlocal other_ran
        monkeypatch.setattr(server, "coordinator", coordinator)
        try:
            await uncertain_worker.run(
                lambda: None,
                budget=OperationBudget.start(1.0),
                operation="uncertain worker warmup",
            )

            async def uncertain() -> None:
                await server._run_native_mutation(
                    blocking_mutation,
                    budget=OperationBudget.start(0.1),
                    operation="uncertain mutation",
                    worker=uncertain_worker,
                )

            with pytest.raises(server._NativeMutationOutcomeUnknown):
                await coordinator.execute_foreground(
                    uncertain,
                    operation_manages_deadline=True,
                )
            assert started.is_set()

            async def other_mutation() -> None:
                nonlocal other_ran
                await other_worker.run(
                    lambda: None,
                    budget=OperationBudget.start(1.0),
                    operation="other provider mutation",
                )
                other_ran = True

            with pytest.raises(OperationError) as exc_info:
                await coordinator.execute_foreground(other_mutation)
            assert exc_info.value.code is OperationErrorCode.PROVIDER_BUSY
            assert other_ran is False

            release.set()
            await uncertain_worker.wait_until_idle()
            while coordinator._foreground_poison:
                await asyncio.sleep(0)

            await coordinator.execute_foreground(other_mutation)
            assert other_ran is True
        finally:
            release.set()
            await uncertain_worker.aclose()
            await other_worker.aclose()

    asyncio.run(run())


def test_foreground_mutation_preserves_ordinary_success_and_failure(monkeypatch):
    from agent_eyes import server

    worker = ProviderWorker("press-result-test")
    backend = MagicMock()
    backend.is_available.return_value = True
    backend.press_key.side_effect = [True, False]
    monkeypatch.setattr(server, "input_worker", worker)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "_input_backend", backend)

    async def run() -> None:
        try:
            succeeded = await server._handle_press_key({"key": "Enter"})
            failed = await server._handle_press_key({"key": "Enter"})
            assert succeeded == "pressed Enter"
            assert "Could not press key" in failed
            assert backend.press_key.call_count == 2
        finally:
            await worker.aclose()

    asyncio.run(run())


def test_late_apple_shadow_mutation_is_unknown_and_not_replayed(monkeypatch):
    from agent_eyes import server

    worker = ProviderWorker("late-apple-shadow-test")
    started = threading.Event()
    release = threading.Event()
    applied = threading.Event()
    calls = 0

    def late_apple_action(_args: dict) -> str:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2.0)
        applied.set()
        return "Shadow click completed."

    monkeypatch.setattr(server, "apple_worker", worker)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server, "_handle_shadow", late_apple_action)

    async def run() -> None:
        try:
            await worker.run(
                lambda: None,
                budget=OperationBudget.start(1.0),
                operation="late Apple shadow worker warmup",
            )
            output = await server._handle_shadow_async(
                {
                    "action": "click",
                    "target_id": "apple-events:browser:window:tab",
                    "_operation_budget": OperationBudget.start(0.1),
                }
            )
            assert "OUTCOME_UNKNOWN" in output
            assert started.is_set()
            assert calls == 1
            assert worker.busy is True

            release.set()
            await worker.wait_until_idle()
            assert applied.is_set()
            assert calls == 1
        finally:
            release.set()
            await worker.wait_until_idle()
            await worker.aclose()

    asyncio.run(run())


def test_native_tree_provider_work_runs_off_event_loop_thread(monkeypatch):
    from agent_eyes import server

    worker = ProviderWorker("heartbeat-test")
    provider_thread_ids: list[int] = []
    adapter = MagicMock()
    adapter.get_tree.side_effect = lambda *args, **kwargs: (
        provider_thread_ids.append(threading.get_ident()) or _native_tree("slow")
    )
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "native_worker", worker)
    monkeypatch.setattr(server, "coordinator", AutomationCoordinator())
    monkeypatch.setattr(server._pu, "is_browser_pid", lambda pid: False)

    async def run() -> None:
        event_loop_thread_id = threading.get_ident()
        try:
            output = await server._handle_get_tree({"pid": 42})
            assert "snapshot=" in output
            assert provider_thread_ids
            assert all(
                thread_id != event_loop_thread_id for thread_id in provider_thread_ids
            )
        finally:
            await worker.aclose()

    asyncio.run(run())


def test_tree_schema_exposes_one_total_timeout():
    from agent_eyes.server import TOOLS

    tree = next(tool for tool in TOOLS if tool.name == "tree")
    timeout = tree.inputSchema["properties"]["timeout"]

    assert timeout["default"] == 5.0
    assert timeout["minimum"] == 0
    assert timeout["maximum"] == 30


def test_native_type_never_sends_physical_input_to_a_different_focused_element(
    monkeypatch,
):
    from agent_eyes import server

    class InlineWorker:
        async def run(self, call, **_kwargs):
            return call()

        async def wait_until_idle(self):
            return None

    target = UIElement(
        id=71,
        role="textfield",
        name="Target",
        bounds=(10, 10, 100, 30),
        platform_ref=object(),
        source="native",
        pid=42,
    )
    other = UIElement(
        id=72,
        role="textfield",
        name="Other",
        platform_ref=object(),
        source="native",
        pid=42,
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.focus_element.return_value = True
    adapter.get_focused_element.return_value = other
    adapter.is_same_element.return_value = False
    backend = MagicMock()
    backend.is_available.return_value = True
    worker = InlineWorker()

    async def action_until(_pid, action, condition, **_kwargs):
        return SimpleNamespace(
            action_result=action(),
            condition_met=condition(),
        )

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "native_worker", worker)
    monkeypatch.setattr(server, "input_worker", worker)
    monkeypatch.setattr(server, "run_native_action_until", action_until)
    monkeypatch.setattr(server, "_verify_focus", AsyncMock(return_value=(True, "")))

    result = asyncio.run(
        server._handle_type_resolved(
            target.id,
            "must-not-reach-other-field",
            target,
            budget=OperationBudget.start(1.0),
        )
    )

    assert "FOCUS_MISMATCH" in result
    backend.type_text.assert_not_called()
    backend.clear_and_type.assert_not_called()
    backend.click_and_type.assert_not_called()
    adapter.set_value.assert_not_called()


def test_native_coordinate_type_requires_exact_hit_test_identity(monkeypatch):
    from agent_eyes import server

    class InlineWorker:
        active = False

        async def run(self, call, **_kwargs):
            self.active = True
            try:
                return call()
            finally:
                self.active = False

    target = UIElement(
        id=81,
        role="textfield",
        name="Moved target",
        bounds=(10, 20, 100, 30),
        platform_ref=object(),
        source="native",
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.focus_element.return_value = False
    adapter.get_focused_element.return_value = None
    adapter.element_at_position.return_value = UIElement(
        id=82,
        role="textfield",
        platform_ref=object(),
    )
    backend = MagicMock()
    backend.is_available.return_value = True
    worker = InlineWorker()

    def compare_on_provider_lane(_first, _second):
        assert worker.active is True
        return False

    adapter.is_same_element.side_effect = compare_on_provider_lane

    async def action_until(_pid, action, condition, **kwargs):
        return SimpleNamespace(
            action_result=action(),
            condition_met=await kwargs["condition_worker"].run(condition),
        )

    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", backend)
    monkeypatch.setattr(server, "native_worker", worker)
    monkeypatch.setattr(server, "input_worker", worker)
    monkeypatch.setattr(server, "run_native_action_until", action_until)

    result = asyncio.run(
        server._handle_type_resolved(
            target.id,
            "must-not-hit-moved-element",
            target,
            budget=OperationBudget.start(1.0),
        )
    )

    assert "FOCUS_MISMATCH" in result
    backend.click_and_type.assert_not_called()
