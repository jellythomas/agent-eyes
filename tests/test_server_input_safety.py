from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.types import CallToolResult

from agent_eyes.tool_contract import MAX_TYPED_TEXT_CHARS


def _result_text(result) -> str:
    return "\n".join(item.text for item in result.content)


def test_oversized_type_input_fails_before_readiness_or_dispatch(monkeypatch):
    from agent_eyes import server

    readiness = AsyncMock(side_effect=AssertionError("readiness must not run"))
    dispatch = AsyncMock(side_effect=AssertionError("dispatch must not run"))
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)
    monkeypatch.setattr(server, "_dispatch", dispatch)

    secret = "s" * (MAX_TYPED_TEXT_CHARS + 1)
    result = asyncio.run(
        server.call_tool(
            "type",
            {"id": 1, "text": secret},
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "at most" in _result_text(result)
    assert secret not in _result_text(result)
    readiness.assert_not_awaited()
    dispatch.assert_not_awaited()


def test_unexpected_exception_never_leaks_message_to_logs_or_mcp(monkeypatch, caplog):
    from agent_eyes import server

    secret = "TOP-SECRET-TYPED-VALUE"

    def explode(args):
        raise RuntimeError(secret)

    monkeypatch.setattr(server, "_DISPATCH_TABLE", {"status": explode})

    with caplog.at_level(logging.ERROR, logger="agent-eyes"):
        result = asyncio.run(server.call_tool("status", {}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert secret not in _result_text(result)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_unknown_and_extra_arguments_fail_as_bounded_tool_errors():
    from agent_eyes import server

    unknown = asyncio.run(server.call_tool("does_not_exist", {}))
    extra = asyncio.run(server.call_tool("status", {"secret": "do-not-log"}))

    assert isinstance(unknown, CallToolResult)
    assert unknown.isError is True
    assert "Unknown tool" in _result_text(unknown)
    assert isinstance(extra, CallToolResult)
    assert extra.isError is True
    assert "arguments.secret is not allowed" in _result_text(extra)
    assert "do-not-log" not in _result_text(extra)


def test_handler_error_sentinel_is_reported_as_mcp_tool_error(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(
        server,
        "_dispatch",
        AsyncMock(return_value="ERROR: provider rejected the operation"),
    )

    result = asyncio.run(server.call_tool("status", {}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert _result_text(result) == "ERROR: provider rejected the operation"


def test_every_server_schema_has_runtime_size_or_range_bounds():
    from agent_eyes import server

    def walk(schema):
        yield schema
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            for child in schema.get("properties", {}).values():
                yield from walk(child)
        elif schema.get("type") == "array":
            assert "maxItems" in schema
            if isinstance(schema.get("items"), dict):
                yield from walk(schema["items"])

    for tool in server.TOOLS:
        for schema in walk(tool.inputSchema):
            if schema.get("type") == "string":
                assert "maxLength" in schema
            elif schema.get("type") in {"integer", "number"}:
                assert "minimum" in schema
                assert "maximum" in schema


def test_call_tool_accepts_bounded_shadow_target_id(monkeypatch):
    from agent_eyes import server

    readiness = AsyncMock(return_value=SimpleNamespace(core_ready=True))
    dispatch = AsyncMock(return_value="ok")
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)
    monkeypatch.setattr(server, "_dispatch", dispatch)

    result = asyncio.run(
        server.call_tool(
            "web_tree",
            {"shadow": True, "target_id": "target-7"},
        )
    )

    assert _result_text(SimpleNamespace(content=result)) == "ok"
    dispatch.assert_awaited_once_with(
        "web_tree",
        {"shadow": True, "target_id": "target-7"},
    )
    readiness.assert_not_awaited()


def test_web_tree_without_explicit_shadow_contract_fails_before_readiness(monkeypatch):
    from agent_eyes import server

    readiness = AsyncMock(side_effect=AssertionError("readiness must not run"))
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)

    result = asyncio.run(server.call_tool("web_tree", {}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "Invalid input" in _result_text(result)
    assert "required" in _result_text(result)
    readiness.assert_not_awaited()


def test_dual_mode_shadow_calls_require_target_before_readiness_or_dispatch(monkeypatch):
    from agent_eyes import server

    readiness = AsyncMock(side_effect=AssertionError("readiness must not run"))
    dispatch = AsyncMock(side_effect=AssertionError("dispatch must not run"))
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)
    monkeypatch.setattr(server, "_dispatch", dispatch)

    cases = {
        "navigate": {"url": "https://example.test", "shadow": True},
        "press_key": {"key": "Enter", "shadow": True},
        "wait": {"shadow": True},
        "close_tab": {"shadow": True},
        "scroll": {"shadow": True},
        "drag": {
            "from_x": 1,
            "from_y": 2,
            "to_x": 3,
            "to_y": 4,
            "shadow": True,
        },
    }
    for name, arguments in cases.items():
        result = asyncio.run(server.call_tool(name, arguments))
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert "arguments.target_id is required" in _result_text(result)

    readiness.assert_not_awaited()
    dispatch.assert_not_awaited()


def test_mixed_or_unguarded_pointer_modes_fail_before_readiness_or_dispatch(
    monkeypatch,
):
    from agent_eyes import server

    readiness = AsyncMock(side_effect=AssertionError("readiness must not run"))
    dispatch = AsyncMock(side_effect=AssertionError("dispatch must not run"))
    monkeypatch.setattr(server, "_ensure_runtime_readiness", readiness)
    monkeypatch.setattr(server, "_dispatch", dispatch)

    cases = (
        ("click", {"x": 10, "y": 20}),
        (
            "click",
            {
                "id": 4,
                "snapshot": "s-live",
                "x": 10,
                "y": 20,
                "pid": 7,
                "shadow": True,
            },
        ),
        (
            "wait",
            {
                "shadow": True,
                "target_id": "tab-7",
                "pid": 7,
                "name": "Ready",
            },
        ),
        (
            "hover",
            {"id": 4, "snapshot": "n-live", "x": 10, "y": 20},
        ),
    )

    for name, arguments in cases:
        result = asyncio.run(server.call_tool(name, arguments))
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert "Invalid input" in _result_text(result)

    readiness.assert_not_awaited()
    dispatch.assert_not_awaited()
