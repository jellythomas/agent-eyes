from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from agent_eyes.cdp import CDPClient, ChromeTab as LegacyChromeTab
from agent_eyes.cdp_persistent import CDPConnection, CDPSession
from agent_eyes.input_sim import MacOSInputBackend, WindowsInputBackend
from agent_eyes.native_events import (
    NativeChangeSubscription,
    run_native_action_until,
    wait_for_native_element,
)


class _FailingAsyncContext:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> object:
        raise self._error

    async def __aexit__(self, *args: object) -> None:
        return None


class _OneMessageWebSocket:
    def __init__(self, message: dict[str, object]) -> None:
        self._message = json.dumps(message)
        self._sent = False

    def __aiter__(self) -> _OneMessageWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return self._message


def test_legacy_cdp_logs_do_not_disclose_ws_hosts_or_exception_messages(
    monkeypatch,
    caplog,
):
    import websockets
    from agent_eyes import cdp as cdp_module

    host_secret = "sentinel-legacy-ws-host.invalid"
    error_secret = "sentinel-legacy-dialog-exception"
    payload = [
        {
            "id": "page-1",
            "type": "page",
            "title": "Example",
            "url": "https://example.test",
            "webSocketDebuggerUrl": f"ws://{host_secret}/devtools/page/1",
        }
    ]
    monkeypatch.setattr(
        cdp_module,
        "_cdp_http_get",
        lambda *_args, **_kwargs: (200, json.dumps(payload).encode()),
    )
    monkeypatch.setattr(
        websockets,
        "connect",
        lambda *_args, **_kwargs: _FailingAsyncContext(RuntimeError(error_secret)),
    )
    client = CDPClient()
    tab = LegacyChromeTab(
        id="page-1",
        title="Example",
        url="https://example.test",
        ws_url="ws://127.0.0.1:9222/devtools/page/1",
    )

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        assert asyncio.run(client.list_tabs()) == []
        assert asyncio.run(client.handle_dialog(tab)) is False

    assert host_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_persistent_event_handler_log_redacts_method_and_exception(caplog):
    method_secret = "Sentinel.provider.secretEvent"
    error_secret = "sentinel-persistent-handler-exception"
    session = CDPSession("session", CDPConnection())

    def fail(_params: dict[str, object]) -> None:
        raise RuntimeError(error_secret)

    session.on_event(method_secret, fail)

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        session._on_message({"method": method_secret, "params": {}})

    assert method_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_persistent_target_and_session_logs_redact_provider_identifiers(caplog):
    non_page_target = "ZXQNONPAGE-target-credential"
    target_type = "ZXQWORKER-provider-type"
    target_id = "ZXQTARGET-target-credential"
    session_id = "ZXQSESSION-session-credential"
    conn = CDPConnection()

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        conn._on_attached(
            {
                "sessionId": "non-page-session",
                "targetInfo": {
                    "targetId": non_page_target,
                    "type": target_type,
                },
            }
        )
        conn._on_attached(
            {
                "sessionId": session_id,
                "targetInfo": {
                    "targetId": target_id,
                    "type": "page",
                    "title": "",
                    "url": "",
                    "webSocketDebuggerUrl": "",
                },
            }
        )
        conn._on_detached({"sessionId": session_id, "targetId": target_id})

    assert non_page_target not in caplog.text
    assert target_type not in caplog.text
    assert target_id[:8] not in caplog.text
    assert session_id[:8] not in caplog.text


def test_persistent_unknown_session_log_redacts_provider_identifier(caplog):
    session_secret = "sentinel-unknown-session-credential"

    async def run() -> None:
        conn = CDPConnection()
        conn._ws = _OneMessageWebSocket(
            {
                "sessionId": session_secret,
                "method": "Page.loadEventFired",
                "params": {},
            }
        )
        await conn._read_loop()

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        asyncio.run(run())

    assert session_secret not in caplog.text


def test_persistent_endpoint_logs_redact_ws_host_and_exception(
    monkeypatch,
    caplog,
):
    from agent_eyes import cdp_persistent as persistent_module

    host_secret = "sentinel-persistent-ws-host.invalid"
    error_secret = "sentinel-version-endpoint-exception"
    conn = CDPConnection()
    responses: list[object] = [
        (
            200,
            json.dumps({
                "webSocketDebuggerUrl": (
                    f"ws://{host_secret}/devtools/browser/credential"
                )
            }).encode(),
        ),
        RuntimeError(error_secret),
    ]

    def http_get(*_args: object, **_kwargs: object) -> tuple[int, bytes]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, tuple)
        return response

    monkeypatch.setattr(persistent_module, "_cdp_http_get", http_get)

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        assert asyncio.run(conn._get_browser_ws_url(9222)) is None
        assert asyncio.run(conn._get_browser_ws_url(9222)) is None

    assert host_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_input_logs_redact_unknown_keys_and_backend_exception_messages(caplog):
    key_secret = "sentinel-unknown-key-credential"
    error_secret = "sentinel-windows-input-exception"
    macos = MacOSInputBackend()
    macos._quartz = object()
    windows = WindowsInputBackend.__new__(WindowsInputBackend)
    windows._load = lambda: None

    def fail(_virtual_key: int) -> None:
        raise RuntimeError(error_secret)

    windows._tap_vk = fail

    with caplog.at_level(logging.ERROR, logger="agent-eyes"):
        assert macos.press_key(key_secret) is False
        assert windows.press_key("return") is False

    assert key_secret not in caplog.text
    assert error_secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_native_event_logs_redact_registration_exception_messages(caplog):
    thread_error_secret = "sentinel-native-thread-exception"
    action_error_secret = "sentinel-native-action-registration-exception"
    wait_error_secret = "sentinel-native-wait-registration-exception"
    subscription = NativeChangeSubscription(17)

    def fail_thread() -> None:
        raise RuntimeError(thread_error_secret)

    subscription._run_backend = fail_thread

    async def run() -> None:
        element = object()

        async def fail_action_factory(_pid: int) -> None:
            raise RuntimeError(action_error_secret)

        async def fail_wait_factory(_pid: int) -> None:
            raise RuntimeError(wait_error_secret)

        result = await run_native_action_until(
            18,
            lambda: True,
            lambda: True,
            timeout=0.1,
            subscription_factory=fail_action_factory,
        )
        assert result.condition_met is True
        wait_result = await wait_for_native_element(
            SimpleNamespace(
                find_elements=lambda *_args, **_kwargs: [element],
            ),
            19,
            timeout=1.0,
            subscription_factory=fail_wait_factory,
        )
        assert wait_result.element is element

    with caplog.at_level(logging.DEBUG, logger="agent-eyes.native-events"):
        subscription._thread_entry()
        asyncio.run(run())

    assert thread_error_secret not in caplog.text
    assert action_error_secret not in caplog.text
    assert wait_error_secret not in caplog.text
    messages = {
        record.getMessage()
        for record in caplog.records
        if record.name == "agent-eyes.native-events"
    }
    assert {
        "Native event registration failed for PID 17 "
        "(exception_type=RuntimeError)",
        "Native action events unavailable for PID 18 "
        "(exception_type=RuntimeError)",
        "Native events unavailable for PID 19 (exception_type=RuntimeError)",
    } <= messages


def test_applescript_tab_matching_logs_redact_urls_and_titles(monkeypatch, caplog):
    from agent_eyes import server

    url_secret = "https://example.test/callback?code=oauth-secret-sentinel"
    title_secret = "private-title-sentinel"
    monkeypatch.setattr(
        server,
        "_cached_tabs",
        [SimpleNamespace(url=url_secret, title=title_secret)],
    )
    monkeypatch.setattr(
        server,
        "_get_applescript_tabs",
        lambda: [
            SimpleNamespace(
                url="https://different.test/?token=other-secret-sentinel",
                title="different-private-title",
                window_index=0,
                index=0,
            )
        ],
    )

    with caplog.at_level(logging.DEBUG, logger="agent-eyes"):
        server._resolve_applescript_tab(0)

    assert "oauth-secret-sentinel" not in caplog.text
    assert "private-title-sentinel" not in caplog.text
    assert "other-secret-sentinel" not in caplog.text
