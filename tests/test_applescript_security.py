from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_eyes import applescript


@pytest.mark.parametrize(
    "payload",
    [
        '"); Application("Finder").quit(); //',
        "line one\nline two\\tail\r\u2028",
        "' OR role=*]",
    ],
)
def test_shadow_execute_script_encodes_javascript_as_data(payload: str):
    baseline = applescript._build_shadow_execute_script(
        "safe",
        tab_index=2,
        window_index=1,
    )
    script = applescript._build_shadow_execute_script(
        payload,
        tab_index=2,
        window_index=1,
    )

    encoded_payload = json.dumps(payload)
    assert script.count(encoded_payload) == 1
    assert script.replace(encoded_payload, "<DATA>") == baseline.replace(
        json.dumps("safe"),
        "<DATA>",
    )


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_shadow_execute_script_rejects_non_integer_indices(value):
    with pytest.raises(ValueError):
        applescript._build_shadow_execute_script(
            "1 + 1",
            tab_index=value,
            window_index=0,
        )


def test_shadow_execute_uses_jxa_stdin_and_keeps_payload_out_of_argv(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setattr(applescript.subprocess, "run", fake_run)

    payload = '"); tell application "Finder" to quit --'
    result = applescript.shadow_execute_js(payload, tab_index=3, window_index=4)

    assert result == "ok"
    args, kwargs = calls[0]
    assert args == ["osascript", "-l", "JavaScript"]
    assert all(payload not in argument for argument in args)
    assert json.dumps(payload) in kwargs["input"]
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 10


def test_shadow_mutation_timeout_is_not_collapsed_into_definite_failure(monkeypatch):
    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setattr(
        applescript,
        "_run_osascript",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("osascript", 10)
        ),
    )

    outcome = applescript.shadow_click_outcome("button")

    assert outcome.status is applescript.ShadowExecutionStatus.OUTCOME_UNKNOWN
    assert outcome.value is None


def test_navigation_timeout_is_not_collapsed_into_definite_failure(monkeypatch):
    monkeypatch.setattr(
        applescript,
        "_run_osascript",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("osascript", 10)
        ),
    )

    outcome = applescript.navigate_tab_outcome(
        "https://example.test",
        tab_id="tab-a",
        window_id="window-a",
    )

    assert outcome.status is applescript.ShadowExecutionStatus.OUTCOME_UNKNOWN


def test_invalid_navigation_target_is_rejected_before_dispatch(monkeypatch):
    runner = MagicMock(side_effect=AssertionError("osascript was dispatched"))
    monkeypatch.setattr(applescript, "_run_osascript", runner)

    outcome = applescript.navigate_tab_outcome(
        "https://example.test",
        tab_index=-1,
    )

    assert outcome.status is applescript.ShadowExecutionStatus.NOT_DISPATCHED
    runner.assert_not_called()


def test_shadow_confirmed_not_found_remains_distinct_from_provider_failure(monkeypatch):
    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setattr(
        applescript,
        "_run_osascript",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="not found\n",
            stderr="",
        ),
    )

    outcome = applescript.shadow_click_outcome("button")

    assert outcome.status is applescript.ShadowExecutionStatus.CONFIRMED
    assert outcome.value == "not found"


def test_invalid_shadow_scroll_is_classified_before_dispatch(monkeypatch):
    runner = MagicMock(side_effect=AssertionError("osascript was dispatched"))
    monkeypatch.setattr(applescript, "_run_osascript", runner)

    outcome = applescript.shadow_scroll_outcome(direction="sideways")

    assert outcome.status is applescript.ShadowExecutionStatus.NOT_DISPATCHED
    runner.assert_not_called()


def test_shadow_type_requires_acknowledgement_and_observed_change():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the generated-JavaScript regression POC")

    script = applescript._shadow_type_script("new text", "#field")
    program = (
        'const element = {value:"before", focus(){}};'
        'const document = {'
        'activeElement:element,'
        'querySelector(){return element;},'
        'execCommand(){element.value="new text"; return false;}'
        '};'
        f'process.stdout.write(String(eval({json.dumps(script)})));'
    )

    completed = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.stdout == "rejected"


def test_shadow_read_never_serializes_password_value():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the generated-JavaScript regression POC")

    secret = "TOP-SECRET-PASSWORD"
    script = applescript._shadow_read_interactive_script()
    program = (
        'const element = {'
        'offsetParent:{},tagName:"INPUT",value:' + json.dumps(secret) + ','
        'textContent:"",placeholder:"",type:"password",'
        'getAttribute(name){return name==="type"?"password":'
        'name==="aria-label"?"Account password":"";},'
        'getBoundingClientRect(){return {x:1,y:2,width:100,height:20};}'
        '};'
        'const document={querySelectorAll(){return [element];}};'
        f'process.stdout.write(String(eval({json.dumps(script)})));'
    )

    completed = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert secret not in completed.stdout
    assert "Account password" in completed.stdout


def test_user_url_selector_and_text_never_appear_in_osascript_argv(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        program = kwargs["input"]
        if "JSON.stringify" in program and "newTab" in program:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "index": 1,
                        "title": "new",
                        "url": "https://example.test/private",
                        "window_index": 0,
                        "id": "tab-new",
                        "window_id": "window-a",
                    }
                ),
                stderr="",
            )
        if "JSON.stringify" in program and "title: tab.title" in program:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"title": "moved", "url": "https://example.test/private"}
                ),
                stderr="",
            )
        if "insertText" in program:
            return SimpleNamespace(returncode=0, stdout="typed\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="clicked\n", stderr="")

    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setattr(applescript.subprocess, "run", fake_run)

    url = 'https://example.test/private?q=");quit()//'
    selector = 'input[value="];quit()//"]'
    text = 'private text ");quit()//'
    role = 'button");quit()//'

    assert applescript.open_new_tab(url) is not None
    assert not applescript.navigate_tab(url, 0, 0).startswith("ERROR")
    assert applescript.shadow_click(selector) is True
    assert applescript.shadow_click_by_text(text, role=role) == "clicked"
    assert applescript.shadow_type(text, selector=selector) is True

    assert calls
    for args, kwargs in calls:
        assert args == ["osascript", "-l", "JavaScript"]
        assert all(
            value not in argument
            for value in (url, selector, text, role)
            for argument in args
        )
        assert kwargs["input"]


def test_tab_inventory_preserves_browser_ids_for_duplicate_urls(monkeypatch):
    duplicate_url = "https://example.test/same"
    data = [
        {
            "window_index": 0,
            "index": 0,
            "title": "first",
            "url": duplicate_url,
            "id": "tab-id-a",
            "window_id": "window-id-a",
        },
        {
            "window_index": 1,
            "index": 0,
            "title": "second",
            "url": duplicate_url,
            "id": "tab-id-b",
            "window_id": "window-id-b",
        },
    ]
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )

    monkeypatch.setattr(applescript.subprocess, "run", fake_run)

    tabs = applescript._list_tabs_jxa()

    assert [tab.url for tab in tabs] == [duplicate_url, duplicate_url]
    assert [tab.id for tab in tabs] == ["tab-id-a", "tab-id-b"]
    assert [tab.window_id for tab in tabs] == ["window-id-a", "window-id-b"]
    assert tabs[0].identifier != tabs[1].identifier
    args, kwargs = calls[0]
    assert args == ["osascript", "-l", "JavaScript"]
    assert "tab.id()" in kwargs["input"]
    assert "win.id()" in kwargs["input"]


def test_stable_tab_id_is_encoded_as_data_for_exact_shadow_targeting():
    script = applescript._build_shadow_execute_script(
        "1 + 1",
        tab_index=9,
        window_index=8,
        tab_id='tab-");quit()//',
        window_id='window-");quit()//',
    )

    assert json.dumps('tab-");quit()//') in script
    assert json.dumps('window-");quit()//') in script
    assert "candidate.id()" in script


def test_shadow_builders_encode_selector_text_role_and_key_as_json(monkeypatch):
    scripts: list[str] = []

    def capture(script, tab_index=0, window_index=0):
        scripts.append(script)
        if "KeyboardEvent" in script:
            return "pressed"
        if "insertText" in script:
            return "typed"
        return "clicked"

    monkeypatch.setattr(applescript, "shadow_execute_js", capture)
    selector = 'input[name="] ; globalThis.pwned = true; //"]'
    text = 'hello"; globalThis.pwned = true; //\nworld'
    role = 'button"]); globalThis.pwned = true; //'
    key = 'x"}); globalThis.pwned = true; //'

    assert applescript.shadow_click(selector) is True
    assert applescript.shadow_click_by_text(text, role=role) == "clicked"
    assert applescript.shadow_type(text, selector=selector) is True
    assert applescript.shadow_press_key(key) is True

    assert json.dumps(selector) in scripts[0]
    assert json.dumps(text) in scripts[1]
    assert json.dumps(role) in scripts[1]
    assert json.dumps(selector) in scripts[2]
    assert json.dumps(text) in scripts[2]
    assert json.dumps(key) in scripts[3]


def test_shadow_scroll_rejects_source_text_and_unknown_direction(monkeypatch):
    calls = []
    monkeypatch.setattr(
        applescript,
        "shadow_execute_js",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert applescript.shadow_scroll(amount="1); globalThis.pwned = true") is False
    assert applescript.shadow_scroll(direction="sideways") is False
    assert calls == []


def test_shadow_navigation_has_no_activation_or_fixed_delay(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(applescript.subprocess, "run", fake_run)

    result = applescript.navigate_tab(
        "https://example.test",
        tab_id="tab-a",
        window_id="window-a",
    )

    assert result == "Navigation dispatched."
    script = calls[0][1]["input"]
    assert "delay(" not in script
    assert "chrome.activate" not in script


def test_apple_event_errors_never_reflect_provider_stderr(monkeypatch):
    sentinel = "PRIVATE_PROVIDER_VALUE_7f3c"
    monkeypatch.setattr(
        applescript.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=sentinel,
        ),
    )

    navigation = applescript.navigate_tab("https://example.test")
    evaluation = applescript.execute_javascript("1 + 1")

    assert sentinel not in navigation
    assert sentinel not in evaluation
    assert navigation == "ERROR: Apple Events navigation failed"
    assert evaluation == "ERROR: Apple Events JavaScript failed"
