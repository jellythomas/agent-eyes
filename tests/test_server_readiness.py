from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.types import CallToolResult

from agent_eyes import __version__
from agent_eyes.setup.readiness import CapabilityProbe, probe_readiness


class AvailableNative:
    def is_available(self):
        return True

    def check_permissions(self):
        return True, "granted"


class AvailableInput:
    def is_available(self):
        return True


class MutableNative:
    def __init__(self, permitted: bool):
        self.permitted = permitted
        self.permission_checks = 0

    def is_available(self):
        return True

    def check_permissions(self):
        self.permission_checks += 1
        return self.permitted, "granted" if self.permitted else "permission missing"


def blocked_report():
    return probe_readiness(
        native_provider=None,
        input_provider=AvailableInput(),
        persistent_executable=Path("/stable/agent-eyes"),
    )


def degraded_report():
    return probe_readiness(
        native_provider=AvailableNative(),
        input_provider=AvailableInput(),
        persistent_executable=Path("/stable/agent-eyes"),
        optional_providers=(
            CapabilityProbe(
                name="browser_bridge",
                required=False,
                status="missing",
                detail="not connected",
            ),
        ),
    )


def permission_required_report():
    return probe_readiness(
        native_provider=MutableNative(permitted=False),
        input_provider=AvailableInput(),
        persistent_executable=Path("/stable/agent-eyes"),
    )


def test_server_identifies_agent_eyes_version():
    from agent_eyes import server

    assert server.app.name == "agent-eyes"
    assert server.app.version == __version__


def test_server_has_no_runtime_dependency_installer():
    from agent_eyes import server

    assert not hasattr(server, "_auto_install_platform_deps")


def test_server_removed_first_tool_setup_preamble():
    from agent_eyes import server

    assert not hasattr(server, "_maybe_auto_setup")


def test_tools_list_remains_static_when_runtime_requires_setup(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_runtime_readiness", blocked_report())
    tools = asyncio.run(server.list_tools())

    assert tools is server.TOOLS
    assert {tool.name for tool in tools} >= {"status", "tree", "list_tabs"}


def test_degraded_core_still_advertises_runtime_tools(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_runtime_readiness", degraded_report())
    tools = asyncio.run(server.list_tools())

    assert len(tools) > 1
    assert "tree" in {tool.name for tool in tools}


def test_blocked_runtime_action_is_a_real_tool_error(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_runtime_readiness", blocked_report())
    result = asyncio.run(server.call_tool("tree", {"pid": 1}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "setup_required" in result.content[0].text
    assert "agent-eyes setup" in result.content[0].text


def test_blocked_runtime_action_reports_actual_readiness_status(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "_runtime_readiness", permission_required_report())
    result = asyncio.run(server.call_tool("tree", {"pid": 1}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.content[0].text.startswith("permission_required:")


def test_status_is_derived_from_live_readiness(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "native_adapter", None)
    monkeypatch.setattr(server, "_input_backend", AvailableInput())
    monkeypatch.setattr(server, "_runtime_readiness", None)
    monkeypatch.setattr(server, "_get_native_adapter", lambda: None)

    status = asyncio.run(server._handle_status())
    install_check = asyncio.run(server._handle_install_check())

    assert status.startswith("setup_required")
    assert install_check == status


def test_status_and_install_check_refresh_permission_state_without_reconnect(monkeypatch):
    from agent_eyes import server

    native = MutableNative(permitted=False)
    monkeypatch.setattr(server, "native_adapter", native)
    monkeypatch.setattr(server, "_input_backend", AvailableInput())
    monkeypatch.setattr(server, "_runtime_readiness", blocked_report())

    assert asyncio.run(server._handle_status()).startswith("permission_required")
    native.permitted = True
    assert asyncio.run(server._handle_install_check()).startswith("ready")
    assert native.permission_checks == 2


def test_status_refresh_does_not_probe_browser_connections(monkeypatch):
    from agent_eyes import server

    def unexpected_browser_probe(*args, **kwargs):
        raise AssertionError("status attempted a browser connection")

    native = MutableNative(permitted=True)
    monkeypatch.setattr(server, "native_adapter", native)
    monkeypatch.setattr(server, "_input_backend", AvailableInput())
    monkeypatch.setattr(server, "_runtime_readiness", None)
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", unexpected_browser_probe)
    monkeypatch.setattr(server.cdp_client, "list_tabs", unexpected_browser_probe)

    assert asyncio.run(server._handle_status()).startswith("ready")
    assert native.permission_checks == 1


def test_blocked_install_check_can_refresh_readiness(monkeypatch):
    from agent_eyes import server

    native = MutableNative(permitted=False)
    monkeypatch.setattr(server, "native_adapter", native)
    monkeypatch.setattr(server, "_input_backend", AvailableInput())
    monkeypatch.setattr(server, "_runtime_readiness", blocked_report())

    result = asyncio.run(server.call_tool("install_check", {}))

    assert isinstance(result, list)
    assert result[0].text.startswith("permission_required")


def test_main_does_not_recheck_permissions_before_stdio_handshake(monkeypatch):
    from agent_eyes import server

    class UnsafeBeforeHandshake:
        def check_permissions(self):
            raise AssertionError("permission probe ran before MCP handshake")

    ran_server = False

    async def fake_run():
        nonlocal ran_server
        ran_server = True

    monkeypatch.setattr(server, "native_adapter", UnsafeBeforeHandshake())
    monkeypatch.setattr(server, "_run", fake_run)

    server.main()

    assert ran_server is True
