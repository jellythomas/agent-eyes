from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agent_eyes.adapters.base import AppInfo, UIElement
from agent_eyes.browser_inventory import (
    BrowserTarget,
    activate_browser_target,
    best_browser_target,
    collect_browser_targets,
    extract_tab_elements,
    format_browser_targets,
    is_browser_app,
    rank_browser_targets,
    sanitize_url_for_display,
)


class FakeAdapter:
    def __init__(self, apps: list[AppInfo], trees: dict[int, UIElement | None]):
        self.apps = apps
        self.trees = trees
        self.tree_calls: list[tuple[int, int]] = []

    def list_apps(self) -> list[AppInfo]:
        return self.apps

    def get_tree(self, pid: int, max_depth: int = 5) -> UIElement | None:
        self.tree_calls.append((pid, max_depth))
        return self.trees.get(pid)


class MultiWindowAdapter(FakeAdapter):
    def __init__(self, apps: list[AppInfo], window_trees: dict[int, list[UIElement]]):
        super().__init__(apps, {})
        self.window_trees = window_trees
        self.browser_tree_calls: list[tuple[int, int]] = []

    def get_browser_trees(self, pid: int, max_depth: int = 6) -> list[UIElement]:
        self.browser_tree_calls.append((pid, max_depth))
        return self.window_trees.get(pid, [])


def test_browser_detection_is_not_chromium_specific():
    supported = (
        ("Safari", "com.apple.Safari"),
        ("Firefox", "org.mozilla.firefox"),
        ("Google Chrome", "com.google.Chrome"),
        ("Microsoft Edge", "com.microsoft.edgemac"),
        ("Brave Browser", "com.brave.Browser"),
        ("Arc", "company.thebrowser.Browser"),
        ("Vivaldi", "com.vivaldi.Vivaldi"),
        ("Opera", "com.operasoftware.Opera"),
    )

    assert all(is_browser_app(name, bundle_id) for name, bundle_id in supported)
    assert not is_browser_app("BrowserStack Local", "com.browserstack.local")


def test_extract_tab_elements_stops_before_page_content():
    browser_tab = UIElement(id=4, role="tab", name="Agent Eyes repository", states=["selected"])
    page_tab = UIElement(id=8, role="tab", name="Settings inside the web page")
    tree = UIElement(
        id=1,
        role="window",
        name="Browser",
        children=[
            UIElement(id=2, role="toolbar", children=[browser_tab]),
            UIElement(id=6, role="webarea", children=[page_tab]),
        ],
    )

    assert extract_tab_elements(tree) == [browser_tab]


def test_real_shaped_browser_tab_strips_exclude_command_buttons():
    chrome_tab = UIElement(id=4, role="radio button", name="Agent Eyes", states=["selected"])
    chrome_other = UIElement(id=5, role="radio button", name="YouTube")
    new_tab = UIElement(id=6, role="button", name="New Tab", actions=["press"])
    close_tab = UIElement(id=7, role="button", name="Close", actions=["press"])
    menu = UIElement(id=8, role="button", name="Tab actions", actions=["press"])
    chrome = UIElement(
        id=1,
        role="window",
        children=[
            UIElement(
                id=2,
                role="tab group",
                children=[chrome_tab, chrome_other, new_tab, close_tab, menu],
            )
        ],
    )
    safari_tab = UIElement(id=10, role="tab", name="Safari tab")
    firefox_tab = UIElement(id=11, role="radio", name="Firefox tab")
    semantic_button_tab = UIElement(
        id=12,
        role="button",
        name="Button-backed tab",
        description="page tab",
    )
    other = UIElement(
        id=9,
        role="tablist",
        children=[safari_tab, firefox_tab, semantic_button_tab],
    )

    assert extract_tab_elements(chrome) == [chrome_tab, chrome_other]
    assert extract_tab_elements(other) == [safari_tab, firefox_tab, semantic_button_tab]


def test_collects_every_browser_and_uses_window_fallback():
    safari_tab = UIElement(id=3, role="tab", name="OpenAI Docs", states=["selected"])
    adapter = FakeAdapter(
        apps=[
            AppInfo(
                pid=101,
                name="Safari",
                bundle_id="com.apple.Safari",
                windows=["OpenAI Docs"],
                is_frontmost=True,
            ),
            AppInfo(
                pid=202,
                name="Firefox",
                bundle_id="org.mozilla.firefox",
                windows=["Issue tracker", "Release notes"],
            ),
            AppInfo(pid=303, name="Notes", windows=["Personal"]),
        ],
        trees={
            101: UIElement(id=1, role="window", children=[safari_tab]),
            202: UIElement(id=1, role="window", name="Issue tracker"),
        },
    )

    targets = collect_browser_targets(adapter)

    assert [(target.browser, target.title) for target in targets] == [
        ("Safari", "OpenAI Docs"),
        ("Firefox", "Issue tracker"),
        ("Firefox", "Release notes"),
    ]
    assert [target.pid for target in targets] == [101, 202, 202]
    assert adapter.tree_calls == [(101, 8), (202, 8)]


def test_collects_tabs_from_every_native_browser_window():
    first = UIElement(id=3, role="tab", name="First window tab")
    second = UIElement(id=8, role="tab", name="Second window tab")
    adapter = MultiWindowAdapter(
        apps=[AppInfo(pid=51, name="Firefox", windows=["First", "Second"])],
        window_trees={
            51: [
                UIElement(id=1, role="window", children=[first]),
                UIElement(id=6, role="window", children=[second]),
            ]
        },
    )

    targets = collect_browser_targets(adapter)

    assert [target.title for target in targets[:2]] == [
        "First window tab",
        "Second window tab",
    ]
    assert [target.window_index for target in targets[:2]] == [0, 1]
    assert adapter.browser_tree_calls == [(51, 8)]
    assert adapter.tree_calls == []


def test_duplicate_titled_windows_remain_distinct_and_keep_native_identity():
    first_window = UIElement(id=1, role="window", name="Same title", platform_ref=object())
    second_window = UIElement(id=2, role="window", name="Same title", platform_ref=object())
    adapter = MultiWindowAdapter(
        apps=[AppInfo(pid=51, name="Firefox", windows=["Same title", "Same title"])],
        window_trees={51: [first_window, second_window]},
    )

    targets = collect_browser_targets(adapter)

    assert len(targets) == 2
    assert [target.window_index for target in targets] == [0, 1]
    assert [target.window_element for target in targets] == [
        first_window,
        second_window,
    ]
    assert all(target.source == "native-window" for target in targets)


def test_windows_title_is_classified_by_process_name():
    adapter = FakeAdapter(
        apps=[AppInfo(pid=77, name="Agent Eyes - GitHub")],
        trees={77: UIElement(id=1, role="window", name="Agent Eyes - GitHub")},
    )

    with (
        patch("agent_eyes.browser_inventory.sys.platform", "win32"),
        patch("agent_eyes.browser_inventory.get_process_name", return_value="msedge"),
    ):
        targets = collect_browser_targets(adapter)

    assert [(target.browser, target.title) for target in targets] == [
        ("Microsoft Edge", "Agent Eyes - GitHub")
    ]


def test_query_ranking_prefers_reusable_title_and_url_matches():
    targets = [
        BrowserTarget(browser="Safari", pid=1, title="Home", url="https://example.com"),
        BrowserTarget(
            browser="Firefox",
            pid=2,
            title="Agent Eyes pull request",
            url="https://github.com/acme/agent-eyes/pull/17",
        ),
        BrowserTarget(browser="Chrome", pid=3, title="Agent documentation"),
    ]

    ranked = rank_browser_targets(targets, "agent eyes github")

    assert ranked[0].title == "Agent Eyes pull request"
    assert ranked[0].score > ranked[1].score


def test_best_target_requires_a_meaningful_match():
    targets = [BrowserTarget(browser="Safari", pid=1, title="Unrelated news")]

    assert best_browser_target(targets, "agent eyes") is None


def test_url_reuse_requires_same_host_and_path_when_url_is_known_or_requested():
    target = BrowserTarget(browser="Safari", pid=1, title="Google", url="https://google.com")
    assert best_browser_target([target], "https://example.com") is None
    assert best_browser_target([target], "https://google.com") is not None
    title_only = BrowserTarget(browser="Safari", pid=2, title="GitHub")
    assert best_browser_target([title_only], "https://github.com/openai/repo/issues/1") is None


def test_activate_browser_target_focuses_window_and_selects_existing_tab():
    element = UIElement(id=3, role="tab", name="Agent Eyes", actions=["press"])
    window = UIElement(id=2, role="window", name="Browser")
    target = BrowserTarget(
        browser="Firefox",
        pid=51,
        title="Agent Eyes",
        element=element,
        window_element=window,
    )
    adapter = MagicMock()
    adapter.perform_action.return_value = True
    adapter.is_window_focused.side_effect = [False, True]
    adapter.focus_window.return_value = True
    input_provider = MagicMock()
    input_provider.is_available.return_value = True
    input_provider.activate_window.return_value = True

    activated = activate_browser_target(adapter, input_provider, target)

    assert activated is True
    input_provider.activate_window.assert_called_once_with(51)
    adapter.focus_window.assert_called_once_with(window)
    adapter.perform_action.assert_called_once_with(element, "press")


def test_failed_existing_tab_activation_never_implies_new_tab_is_safe():
    element = UIElement(id=3, role="tab", name="Agent Eyes")
    window = UIElement(id=2, role="window", name="Browser")
    target = BrowserTarget(
        browser="Safari",
        pid=51,
        title="Agent Eyes",
        element=element,
        window_element=window,
    )
    adapter = MagicMock()
    adapter.perform_action.return_value = False
    adapter.focus_element.return_value = False
    adapter.is_window_focused.return_value = True
    input_provider = MagicMock()
    input_provider.is_available.return_value = True
    input_provider.activate_window.return_value = True

    assert activate_browser_target(adapter, input_provider, target) is False


def test_query_output_is_compact_but_reports_full_scan_count():
    targets = [
        BrowserTarget(browser="Safari", pid=1, title="OpenAI API docs", selected=True),
        BrowserTarget(browser="Firefox", pid=2, title="Unrelated news"),
    ]

    output = format_browser_targets(targets, query="OpenAI docs", max_query_results=1)

    assert "Scanned 2 open browser targets" in output
    assert "[native:1:w0:h" in output
    assert "OpenAI API docs" in output
    assert "Unrelated news" not in output
    assert "remote-debugging" not in output


def test_unfiltered_output_is_capped_and_retains_selected_target():
    targets = [
        BrowserTarget(browser="Firefox", pid=index, title=f"Tab {index}")
        for index in range(100)
    ]
    targets[-1].selected = True

    output = format_browser_targets(targets, max_query_results=10)

    assert "Scanned 100 open browser targets" in output
    assert "Showing 10 targets; 90 additional targets omitted." in output
    assert "Tab 99" in output
    assert output.count("[native:") == 10


def test_display_url_strips_userinfo_query_values_and_fragment():
    secret_url = (
        "https://oauth-user:password-secret@example.test/oauth/callback"
        "?code=oauth-secret&reset_token=reset-secret"
        "#access_token=fragment-secret"
    )

    rendered = sanitize_url_for_display(secret_url)
    output = format_browser_targets(
        [BrowserTarget(browser="Firefox", pid=1, title="OAuth", url=secret_url)]
    )

    assert rendered == "https://example.test/oauth/callback?redacted"
    for secret in (
        "oauth-user",
        "password-secret",
        "oauth-secret",
        "reset-secret",
        "fragment-secret",
    ):
        assert secret not in output


def test_native_target_identity_does_not_change_when_results_are_ranked():
    target = BrowserTarget(
        browser="Firefox",
        pid=42,
        title="Agent Eyes",
        window_index=2,
        tab_index=5,
    )

    unfiltered = format_browser_targets([target])
    ranked = format_browser_targets([target], query="Agent Eyes")

    assert target.identifier.startswith("native:42:w2:t5:h")
    assert f"[{target.identifier}]" in unfiltered
    assert f"[{target.identifier}]" in ranked


def test_default_server_tab_listing_never_probes_shadow_provider(monkeypatch):
    from agent_eyes import server

    native_targets = [
        BrowserTarget(browser="Safari", pid=42, title="Already open", selected=True)
    ]
    monkeypatch.setattr(server, "native_adapter", object())
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: native_targets)
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock(return_value=True))

    output = asyncio.run(server._handle_list_tabs({"query": "already"}))

    assert "Already open" in output
    server.cdp_pool.ensure_connected.assert_not_awaited()
    server.cdp_client.is_available.assert_not_awaited()


def test_explicit_shadow_tab_listing_may_probe_cdp(monkeypatch):
    from agent_eyes import server

    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())
    monkeypatch.setattr(server.cdp_pool, "_connected", False)
    monkeypatch.setattr(server.cdp_client, "is_available", AsyncMock(return_value=False))
    monkeypatch.setattr(server, "_as", None)

    output = asyncio.run(server._handle_list_tabs({"shadow": True}))

    server.cdp_pool.ensure_connected.assert_awaited_once()
    assert "optional shadow provider" in output.lower()
    assert "restart" not in output.lower()


def test_explicit_shadow_tab_listing_emits_canonical_target_id(monkeypatch):
    from agent_eyes import server

    tab = SimpleNamespace(
        id="target-stable-7",
        title="Open page",
        url="https://example.test",
    )
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())
    monkeypatch.setattr(server.cdp_pool, "_connected", True)
    monkeypatch.setattr(server.cdp_pool, "list_tabs", lambda: [tab])

    output = asyncio.run(server._handle_list_tabs({"shadow": True}))

    assert "target_id=target-stable-7" in output


def test_explicit_shadow_tab_listing_sanitizes_urls_and_caps_results(monkeypatch):
    from agent_eyes import server

    tabs = [
        SimpleNamespace(
            id=f"target-{index}",
            title=f"Open page {index}",
            url=(
                "https://user:password-secret@example.test/callback"
                f"?code=oauth-secret-{index}#access_token=fragment-secret"
            ),
        )
        for index in range(20)
    ]
    monkeypatch.setattr(server, "collect_browser_targets", lambda adapter: [])
    monkeypatch.setattr(server.cdp_pool, "ensure_connected", AsyncMock())
    monkeypatch.setattr(server.cdp_pool, "_connected", True)
    monkeypatch.setattr(server.cdp_pool, "list_tabs", lambda: tabs)

    output = asyncio.run(
        server._handle_list_tabs({"shadow": True, "max_results": 5})
    )

    assert output.count("[shadow:") == 5
    assert "15 additional shadow targets omitted." in output
    assert "password-secret" not in output
    assert "oauth-secret" not in output
    assert "fragment-secret" not in output


def test_full_web_tree_header_sanitizes_url_credentials():
    from agent_eyes import server

    tree = UIElement(id=1, role="document", name="OAuth")
    output = server._format_web_tree_response(
        tree,
        "OAuth",
        "https://user:password-secret@example.test/callback?code=oauth-secret#token=fragment-secret",
        0,
        3,
        False,
        "snapshot-token",
        1,
        frozenset({1}),
    )

    assert "URL: https://example.test/callback?redacted" in output
    assert "password-secret" not in output
    assert "oauth-secret" not in output
    assert "fragment-secret" not in output
