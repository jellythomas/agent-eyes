from __future__ import annotations

import asyncio

from agent_eyes.cdp import CDPClient, SecureDOMMetadata


def _ax_node(
    node_id: str,
    role: str,
    *,
    child_ids: list[str] | None = None,
    backend_id: int | None = None,
    value: str = "",
    properties: list[dict] | None = None,
) -> dict:
    node = {
        "nodeId": node_id,
        "ignored": False,
        "role": {"type": "role", "value": role},
        "childIds": child_ids or [],
        "properties": properties or [],
    }
    if backend_id is not None:
        node["backendDOMNodeId"] = backend_id
    if value:
        node["value"] = {"type": "string", "value": value}
    return node


def test_nonignored_generic_wrapper_flattens_real_ax_descendants():
    client = CDPClient()
    nodes = [
        _ax_node("root", "RootWebArea", child_ids=["wrapper"]),
        _ax_node(
            "wrapper",
            "generic",
            child_ids=["button", "input", "inline"],
        ),
        _ax_node("button", "button", backend_id=101),
        _ax_node("input", "textbox", backend_id=102),
        _ax_node("inline", "InlineTextBox"),
    ]

    tree = client._build_tree(
        nodes,
        secure_metadata=SecureDOMMetadata(complete=True),
    )

    assert tree is not None
    assert tree.role == "RootWebArea"
    assert [child.role for child in tree.children] == ["button", "textbox"]
    assert [child.platform_ref for child in tree.children] == [101, 102]


def test_secure_dom_metadata_is_batched_and_never_reads_control_values():
    async def run():
        calls: list[tuple[str, dict]] = []

        async def send(method: str, params: dict) -> dict:
            calls.append((method, params))
            if method == "DOM.getDocument":
                assert params == {"depth": 0, "pierce": False}
                return {"root": {"nodeId": 1}}
            if method == "DOM.pushNodesByBackendIdsToFrontend":
                assert params == {
                    "backendNodeIds": [101, 102, 105, 103, 104]
                }
                return {"nodeIds": [201, 202, 205, 203, 204]}
            if method == "DOM.performSearch":
                assert params["includeUserAgentShadowDOM"] is True
                assert "password" in params["query"]
                assert "current-password" in params["query"]
                assert "new-password" in params["query"]
                return {"searchId": "secure-search", "resultCount": 3}
            if method == "DOM.getSearchResults":
                assert params == {
                    "searchId": "secure-search",
                    "fromIndex": 0,
                    "toIndex": 3,
                }
                return {"nodeIds": [201, 202, 205]}
            if method == "DOM.getNodesForSubtreeByStyle":
                assert params == {
                    "nodeId": 1,
                    "computedStyles": [
                        {"name": "-webkit-text-security", "value": "circle"},
                        {"name": "-webkit-text-security", "value": "disc"},
                        {"name": "-webkit-text-security", "value": "square"},
                    ],
                    "pierce": True,
                }
                return {"nodeIds": [203]}
            if method == "DOM.discardSearchResults":
                assert params == {"searchId": "secure-search"}
                return {}
            raise AssertionError(method)

        nodes = [
            _ax_node("password", "textbox", backend_id=101, value="pw-secret"),
            _ax_node("current", "textbox", backend_id=102, value="current-secret"),
            _ax_node("new", "textbox", backend_id=105, value="new-secret"),
            _ax_node("styled", "textbox", backend_id=103, value="styled-secret"),
            _ax_node("normal", "textbox", backend_id=104, value="visible value"),
        ]

        metadata = await CDPClient().collect_secure_dom_metadata(send, nodes)

        assert metadata == SecureDOMMetadata(
            secure_backend_node_ids=frozenset({101, 102, 103, 105}),
            complete=True,
        )
        assert [method for method, _params in calls] == [
            "DOM.getDocument",
            "DOM.pushNodesByBackendIdsToFrontend",
            "DOM.performSearch",
            "DOM.getSearchResults",
            "DOM.getNodesForSubtreeByStyle",
            "DOM.discardSearchResults",
        ]
        assert all(
            method not in {"DOM.getAttributes", "DOM.describeNode"}
            for method, _params in calls
        )
        assert calls[0][1] == {"depth": 0, "pierce": False}

    asyncio.run(run())


def test_dom_secure_metadata_redacts_ax_textbox_values_without_ax_secure_state():
    client = CDPClient()
    secret_values = {
        "password": (101, "password-secret-101"),
        "autocomplete": (102, "autocomplete-secret-102"),
        "styled": (103, "styled-secret-103"),
    }
    nodes = [
        _ax_node("root", "RootWebArea", child_ids=["password", "autocomplete", "styled", "normal"]),
        *[
            _ax_node(node_id, "textbox", backend_id=backend_id, value=value)
            for node_id, (backend_id, value) in secret_values.items()
        ],
        _ax_node("normal", "textbox", backend_id=104, value="visible value"),
    ]

    tree = client._build_tree(
        nodes,
        secure_metadata=SecureDOMMetadata(
            secure_backend_node_ids=frozenset(
                backend_id for backend_id, _value in secret_values.values()
            ),
            complete=True,
        ),
    )

    assert tree is not None
    by_backend_id = {child.platform_ref: child for child in tree.children}
    for backend_id, secret in secret_values.values():
        assert by_backend_id[backend_id].value == ""
        assert "secure" in by_backend_id[backend_id].states
        assert secret not in tree.to_text()
    assert by_backend_id[104].value == "visible value"


def test_secure_metadata_maps_exact_ax_candidates_without_dom_tree_scan():
    async def run():
        async def send(method: str, params: dict) -> dict:
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.pushNodesByBackendIdsToFrontend":
                assert params == {"backendNodeIds": [77]}
                return {"nodeIds": [9001]}
            if method == "DOM.performSearch":
                return {"searchId": "secure-search", "resultCount": 0}
            if method == "DOM.getNodesForSubtreeByStyle":
                return {"nodeIds": []}
            if method == "DOM.discardSearchResults":
                return {}
            raise AssertionError(method)

        metadata = await CDPClient().collect_secure_dom_metadata(
            send,
            [_ax_node("field", "textbox", backend_id=77, value="visible")],
        )

        assert metadata == SecureDOMMetadata(complete=True)

    asyncio.run(run())


def test_incomplete_dom_metadata_fails_closed_for_text_values():
    client = CDPClient()
    element = client._node_to_element(
        _ax_node("field", "textbox", backend_id=9, value="unverified-secret")
    )

    assert element is not None
    assert element.value == ""
    assert "value-redacted" in element.states
    assert "unverified-secret" not in element.to_text()


def test_value_bearing_textbox_without_backend_id_keeps_metadata_incomplete():
    async def run():
        async def send(_method: str, _params: dict) -> dict:
            raise AssertionError("unclassifiable nodes must not start DOM queries")

        metadata = await CDPClient().collect_secure_dom_metadata(
            send,
            [_ax_node("field", "textbox", value="unmapped-secret")],
        )

        assert metadata == SecureDOMMetadata()

    asyncio.run(run())


def test_ax_protected_property_redacts_value_without_dom_metadata():
    client = CDPClient()
    element = client._node_to_element(
        _ax_node(
            "field",
            "textbox",
            backend_id=9,
            value="ax-protected-secret",
            properties=[{"name": "protected", "value": {"value": True}}],
        )
    )

    assert element is not None
    assert element.value == ""
    assert "secure" in element.states
    assert "ax-protected-secret" not in element.to_text()
