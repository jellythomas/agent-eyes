from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent_eyes.adapters.base import UIElement
from agent_eyes.cdp import (
    CDPClient,
    CDPDocumentChangedError,
    CDPFocusMismatchError,
    CDPMutationOutcomeUnknown,
    ChromeTab,
)


class _Connection:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _tab() -> ChromeTab:
    return ChromeTab(
        "target-1",
        "Example",
        "https://example.test",
        "ws://127.0.0.1:9222/devtools/page/target-1",
    )


def _element(*, role: str = "button", name: str = "Submit") -> UIElement:
    return UIElement(
        id=1,
        role=role,
        name=name,
        source="cdp",
        platform_ref=42,
    )


def _partial_ax_node(
    *,
    role: str = "button",
    name: str = "Submit",
    focused: bool = False,
) -> dict:
    properties = []
    if focused:
        properties.append(
            {
                "name": "focused",
                "value": {"type": "booleanOrUndefined", "value": True},
            }
        )
    return {
        "nodes": [
            {
                "backendDOMNodeId": 42,
                "ignored": False,
                "role": {"value": role},
                "name": {"value": name},
                "properties": properties,
            }
        ]
    }


def test_send_preserves_protocol_events_that_arrive_before_command_response():
    class WebSocket:
        def __init__(self):
            self.messages: list[str] = []

        async def send(self, raw: str):
            message = json.loads(raw)
            self.messages.extend(
                [
                    json.dumps({"method": "Page.loadEventFired", "params": {}}),
                    json.dumps({"id": message["id"], "result": {"ok": True}}),
                ]
            )

        async def recv(self):
            return self.messages.pop(0)

    async def run():
        client = CDPClient()
        events: list[dict] = []
        result = await client._send(
            WebSocket(),
            "Page.navigate",
            {"url": "https://example.test"},
            event_buffer=events,
        )
        assert result == {"ok": True}
        assert [event["method"] for event in events] == ["Page.loadEventFired"]

    asyncio.run(run())


def test_navigate_consumes_load_event_arriving_before_navigation_response(monkeypatch):
    class WebSocket:
        def __init__(self):
            self.messages: list[str] = []
            self.recv_count = 0

        async def send(self, raw: str):
            message = json.loads(raw)
            method = message["method"]
            if method == "Page.navigate":
                self.messages.append(
                    json.dumps({"method": "Page.loadEventFired", "params": {}})
                )
                result = {"frameId": "frame-1"}
            elif method == "Runtime.evaluate":
                result = {"result": {"value": "Loaded title"}}
            else:
                result = {}
            self.messages.append(json.dumps({"id": message["id"], "result": result}))

        async def recv(self):
            self.recv_count += 1
            if not self.messages:
                raise AssertionError(
                    "navigation waited after its load event was already received"
                )
            return self.messages.pop(0)

    async def run():
        import websockets

        websocket = WebSocket()
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(websocket),
        )
        result = await CDPClient().navigate(_tab(), "https://example.test/next")

        assert result == {
            "url": "https://example.test/next",
            "title": "Loaded title",
        }
        assert not websocket.messages

    asyncio.run(run())


def test_enrichment_uses_only_one_high_value_read_per_element():
    async def run():
        client = CDPClient()
        element = UIElement(id=1, role="button", platform_ref=7)

        async def box(_ws, _backend_node_id):
            await asyncio.sleep(0)
            return (1, 2, 3, 4)

        client._get_box_model = box
        client._get_visual_summary = AsyncMock(
            side_effect=AssertionError(
                "legacy visual enrichment is intentionally skipped"
            )
        )

        enriched = await client._enrich_subtree(object(), element, 1)

        assert enriched == 1
        assert element.bounds == (1, 2, 3, 4)
        assert element.visual == ""
        client._get_visual_summary.assert_not_awaited()

    asyncio.run(run())


def test_enrichment_protocol_work_is_linear_with_one_command_per_element():
    async def run():
        client = CDPClient()
        root = UIElement(id=0, role="document")
        root.children = [
            UIElement(id=index, role="button", platform_ref=index)
            for index in range(1, 61)
        ]

        async def send(_ws, method, params=None, **_kwargs):
            if method == "DOM.getBoxModel":
                return {"model": {"border": [0, 0, 10, 0, 10, 10, 0, 10]}}
            return {}

        client._send = AsyncMock(side_effect=send)
        enriched = await client._enrich_tree(object(), root)

        methods = [call.args[1] for call in client._send.await_args_list]
        assert enriched == 60
        assert methods == ["DOM.enable"] + ["DOM.getBoxModel"] * 60

    asyncio.run(run())


def test_click_rejects_changed_document_before_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"frameTree": {"frame": {"loaderId": "loader-new"}}},
                {"root": {"backendNodeId": 99}},
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPDocumentChangedError):
            await client.click_element(
                _tab(),
                42,
                expected_element=_element(),
                expected_revision=123,
            )

        assert [call.args[1] for call in client._send.await_args_list] == [
            "DOM.enable",
            "Page.getFrameTree",
            "DOM.getDocument",
        ]

    asyncio.run(run())


def test_click_raises_unknown_outcome_after_mutation_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"object": {"objectId": "object-1"}},
                _partial_ax_node(),
                RuntimeError("connection lost after dispatch"),
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPMutationOutcomeUnknown):
            await client.click_element(_tab(), 42, expected_element=_element())

    asyncio.run(run())


@pytest.mark.parametrize(
    ("runtime_result", "expected_error"),
    [
        (
            {"result": {"value": "__agent_eyes_action_stale_v1__"}},
            CDPMutationOutcomeUnknown,
        ),
        (
            {
                "exceptionDetails": {
                    "exception": {"description": "Error: STALE_ELEMENT"}
                }
            },
            CDPMutationOutcomeUnknown,
        ),
    ],
)
def test_click_trusts_only_fixed_stale_status(
    monkeypatch,
    runtime_result,
    expected_error,
):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"object": {"objectId": "object-1"}},
                _partial_ax_node(),
                runtime_result,
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(expected_error):
            await client.click_element(_tab(), 42, expected_element=_element())

        assert [call.args[1] for call in client._send.await_args_list] == [
            "DOM.enable",
            "DOM.resolveNode",
            "Accessibility.getPartialAXTree",
            "Runtime.callFunctionOn",
        ]

    asyncio.run(run())


def test_type_raises_unknown_outcome_after_focus_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"object": {"objectId": "object-1"}},
                _partial_ax_node(role="textbox", name="Comment"),
                RuntimeError("connection lost after focus"),
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPMutationOutcomeUnknown):
            await client.type_text(
                _tab(),
                42,
                "secret",
                expected_element=_element(role="textbox", name="Comment"),
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("focus_responses", "expected_error"),
    [
        (
            [{}, _partial_ax_node(role="textbox", name="Comment")],
            CDPFocusMismatchError,
        ),
        (
            [{"unexpected": True}],
            CDPMutationOutcomeUnknown,
        ),
        (
            [{}, {"nodes": "malformed"}],
            CDPMutationOutcomeUnknown,
        ),
    ],
)
def test_type_never_inserts_text_without_trusted_exact_focus(
    monkeypatch,
    focus_responses,
    expected_error,
):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"object": {"objectId": "object-1"}},
                _partial_ax_node(role="textbox", name="Comment"),
                *focus_responses,
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(expected_error):
            await client.type_text(
                _tab(),
                42,
                "secret",
                expected_element=_element(role="textbox", name="Comment"),
            )

        methods = [call.args[1] for call in client._send.await_args_list]
        assert methods[:3] == [
            "DOM.enable",
            "DOM.resolveNode",
            "Accessibility.getPartialAXTree",
        ]
        assert methods[3] == "DOM.focus"
        assert "Input.insertText" not in methods

    asyncio.run(run())


def test_click_rejects_changed_exact_ax_semantics_before_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"object": {"objectId": "object-1"}},
                _partial_ax_node(name="Delete"),
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPDocumentChangedError):
            await client.click_element(
                _tab(),
                42,
                expected_element=_element(name="Approve"),
            )

        assert [call.args[1] for call in client._send.await_args_list] == [
            "DOM.enable",
            "DOM.resolveNode",
            "Accessibility.getPartialAXTree",
        ]

    asyncio.run(run())


def test_new_tab_raises_unknown_outcome_after_create_target_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client.list_tabs = AsyncMock(return_value=[_tab()])
        client._send = AsyncMock(side_effect=RuntimeError("response lost"))
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPMutationOutcomeUnknown):
            await client.new_tab("https://example.test/new")

    asyncio.run(run())


def test_file_input_rejects_changed_document_before_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                {"frameTree": {"frame": {"loaderId": "loader-new"}}},
                {"root": {"backendNodeId": 99}},
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPDocumentChangedError):
            await client.set_file_input(
                _tab(),
                42,
                ["/tmp/upload.txt"],
                expected_revision=123,
            )

        assert [call.args[1] for call in client._send.await_args_list] == [
            "DOM.enable",
            "Page.getFrameTree",
            "DOM.getDocument",
        ]

    asyncio.run(run())


def test_file_input_raises_unknown_outcome_after_dispatch(monkeypatch):
    async def run():
        import websockets

        client = CDPClient()
        client._send = AsyncMock(
            side_effect=[
                {},
                RuntimeError("connection lost after dispatch"),
            ]
        )
        monkeypatch.setattr(
            websockets,
            "connect",
            lambda _url, **_kwargs: _Connection(object()),
        )

        with pytest.raises(CDPMutationOutcomeUnknown):
            await client.set_file_input(_tab(), 42, ["/tmp/upload.txt"])

    asyncio.run(run())
