"""macOS AppleScript helpers for Chrome browser interaction.

Provides fallback access to Chrome tabs and web content WITHOUT requiring
--remote-debugging-port=9222. Uses macOS native AppleScript/JXA via osascript.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum


@dataclass
class AppleScriptTab:
    """A Chrome tab discovered via AppleScript."""
    index: int       # 0-based index
    title: str
    url: str
    window_index: int = 0
    id: str = ""
    window_id: str = ""

    @property
    def identifier(self) -> str:
        """Return a provider-qualified identity, stable when Chrome exposes IDs."""
        if self.id:
            window = self.window_id or "unknown-window"
            return f"apple-events:{window}:{self.id}"
        return f"apple-events:index:w{self.window_index}:t{self.index}"

    @property
    def tab_id(self) -> str:
        """Compatibility-friendly name for the browser-provided tab ID."""
        return self.id


class ShadowExecutionStatus(str, Enum):
    """Whether Apple Events confirmed a JavaScript command's outcome."""

    CONFIRMED = "confirmed"
    NOT_DISPATCHED = "not_dispatched"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class ShadowExecutionOutcome:
    """Tri-state result for an Apple Events JavaScript command."""

    status: ShadowExecutionStatus
    value: str | None = None


def _run_osascript(
    script: str,
    *,
    language: str = "AppleScript",
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run an osascript program over stdin so program data never enters argv."""
    if not isinstance(script, str):
        raise TypeError("script must be a string")
    if language not in {"AppleScript", "JavaScript"}:
        raise ValueError("unsupported osascript language")
    args = ["osascript"]
    if language != "AppleScript":
        args.extend(["-l", language])
    return subprocess.run(
        args,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _validated_index(value: int, name: str) -> int:
    """Return a safe zero-based index for source-code generation."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validated_number(value: int | float, name: str) -> int | float:
    """Return a finite JSON-safe number, rejecting booleans and source text."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be a finite number")
    return value


def _validated_identifier(value: str, name: str) -> str:
    """Validate an optional browser-owned identifier before JXA encoding."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if len(value) > 4_096:
        raise ValueError(f"{name} is too long")
    return value


def _build_tab_selection_script(
    *,
    tab_index: int,
    window_index: int,
    tab_id: str = "",
    window_id: str = "",
) -> str:
    """Build JXA that prefers exact browser IDs and falls back to indices."""
    tab = _validated_index(tab_index, "tab_index")
    window = _validated_index(window_index, "window_index")
    stable_tab = _validated_identifier(tab_id, "tab_id")
    stable_window = _validated_identifier(window_id, "window_id")
    return (
        "function stableId(candidate) {\n"
        "  try {\n"
        "    var value = candidate.id();\n"
        '    return value === undefined || value === null ? "" : String(value);\n'
        "  } catch (error) {\n"
        '    return "";\n'
        "  }\n"
        "}\n"
        f"var requestedWindowId = {json.dumps(stable_window)};\n"
        f"var requestedTabId = {json.dumps(stable_tab)};\n"
        "var windows = chrome.windows();\n"
        "var win = null;\n"
        "var tab = null;\n"
        "if (requestedWindowId) {\n"
        "  for (var wi = 0; wi < windows.length; wi++) {\n"
        "    var candidate = windows[wi];\n"
        "    if (stableId(candidate) === requestedWindowId) { win = candidate; break; }\n"
        "  }\n"
        '  if (!win) throw new Error("requested window is unavailable");\n'
        "}\n"
        "if (requestedTabId) {\n"
        "  var candidateWindows = win ? [win] : windows;\n"
        "  for (var cwi = 0; cwi < candidateWindows.length && !tab; cwi++) {\n"
        "    var candidateWindow = candidateWindows[cwi];\n"
        "    var candidateTabs = candidateWindow.tabs();\n"
        "    for (var ti = 0; ti < candidateTabs.length; ti++) {\n"
        "      var candidate = candidateTabs[ti];\n"
        "      if (stableId(candidate) === requestedTabId) {\n"
        "        win = candidateWindow; tab = candidate; break;\n"
        "      }\n"
        "    }\n"
        "  }\n"
        '  if (!tab) throw new Error("requested tab is unavailable");\n'
        "}\n"
        f"if (!win) win = windows[{window}];\n"
        'if (!win) throw new Error("window index is unavailable");\n'
        f"if (!tab) tab = win.tabs()[{tab}];\n"
        'if (!tab) throw new Error("tab index is unavailable");\n'
    )


def _build_shadow_execute_script(
    js_code: str,
    *,
    tab_index: int,
    window_index: int,
    tab_id: str = "",
    window_id: str = "",
) -> str:
    """Build JXA with caller-controlled JavaScript encoded only as data."""
    if not isinstance(js_code, str):
        raise TypeError("js_code must be a string")
    return (
        'var chrome = Application("Google Chrome");\n'
        + _build_tab_selection_script(
            tab_index=tab_index,
            window_index=window_index,
            tab_id=tab_id,
            window_id=window_id,
        )
        + f"var result = tab.execute({{javascript: {json.dumps(js_code)}}});\n"
        'result === undefined ? "" : String(result);'
    )


def is_available() -> bool:
    """Check if AppleScript Chrome access is available (macOS + Chrome running)."""
    if sys.platform != "darwin":
        return False
    try:
        result = _run_osascript(
            'tell application "System Events" to '
            '(name of processes) contains "Google Chrome"',
            timeout=3,
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def list_chrome_tabs() -> list[AppleScriptTab]:
    """List all Chrome tabs using AppleScript. No remote debugging needed."""
    try:
        return _list_tabs_jxa()
    except Exception:
        try:
            return _list_tabs_individual()
        except Exception:
            return []


def _parse_tab_list(raw: str) -> list[AppleScriptTab]:
    """Parse AppleScript tab list output into structured data."""
    if not raw:
        return []

    # AppleScript returns: winIdx, tabIdx, title, url, winIdx, tabIdx, title, url, ...
    # Format: "0, 0, Title One, https://url1, 0, 1, Title Two, https://url2"
    # But titles/urls can contain commas, so we use a JXA approach instead
    # Fall back to per-tab query if bulk parse fails
    try:
        return _list_tabs_jxa()
    except Exception:
        return _list_tabs_individual()


def _list_tabs_jxa() -> list[AppleScriptTab]:
    """Use JavaScript for Automation (JXA) for reliable JSON output."""
    script = '''
    var chrome = Application("Google Chrome");
    var tabs = [];
    function windowId(win) {
        try { return String(win.id()); } catch (error) { return ""; }
    }
    function tabId(tab) {
        try { return String(tab.id()); } catch (error) { return ""; }
    }
    chrome.windows().forEach(function(win, winIdx) {
        var stableWindowId = windowId(win);
        win.tabs().forEach(function(tab, tabIdx) {
            tabs.push({
                window_index: winIdx,
                index: tabIdx,
                title: tab.title(),
                url: tab.url(),
                id: tabId(tab),
                window_id: stableWindowId
            });
        });
    });
    JSON.stringify(tabs);
    '''
    result = _run_osascript(
        script,
        language="JavaScript",
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    data = json.loads(result.stdout.strip())
    return [
        AppleScriptTab(
            index=item["index"],
            title=item["title"],
            url=item["url"],
            window_index=item["window_index"],
            id=str(item.get("id") or ""),
            window_id=str(item.get("window_id") or ""),
        )
        for item in data
    ]


def _list_tabs_individual() -> list[AppleScriptTab]:
    """Fallback: query tabs one window at a time."""
    # Get window count
    result = _run_osascript(
        'tell application "Google Chrome" to count of windows',
        timeout=5,
    )
    if result.returncode != 0:
        return []

    win_count = int(result.stdout.strip())
    tabs = []
    for win_idx in range(1, win_count + 1):
        script = f'''
        tell application "Google Chrome"
            set w to window {win_idx}
            set winID to "-"
            try
                set winID to (id of w) as text
            end try
            set tabCount to count of tabs of w
            set output to ""
            repeat with i from 1 to tabCount
                set t to tab i of w
                set tabID to "-"
                try
                    set tabID to (id of t) as text
                end try
                set output to output & winID & "\\n" & tabID & "\\n" & title of t & "\\n" & URL of t & "\\n---\\n"
            end repeat
            return output
        end tell
        '''
        result = _run_osascript(script, timeout=10)
        if result.returncode != 0:
            continue

        entries = result.stdout.strip().split("---\n")
        for tab_entry_idx, entry in enumerate(entries):
            lines = entry.strip().split("\n")
            if len(lines) >= 4:
                tabs.append(AppleScriptTab(
                    index=tab_entry_idx,
                    title=lines[2].strip(),
                    url=lines[3].strip(),
                    window_index=win_idx - 1,
                    id="" if lines[1].strip() == "-" else lines[1].strip(),
                    window_id="" if lines[0].strip() == "-" else lines[0].strip(),
                ))

    return tabs


def open_new_tab(url: str = "about:blank") -> AppleScriptTab | None:
    """Open a new tab in Chrome via AppleScript. No remote debugging needed."""
    if not isinstance(url, str):
        return None
    script = f'''
    var chrome = Application("Google Chrome");
    function stableId(candidate) {{
        try {{ return String(candidate.id()); }} catch (error) {{ return ""; }}
    }}
    var win;
    if (chrome.windows.length === 0) {{
        win = chrome.Window().make();
    }} else {{
        win = chrome.windows[0];
    }}
    var tab = chrome.Tab({{url: {json.dumps(url)}}});
    win.tabs.push(tab);
    // Activate the new tab
    win.activeTabIndex = win.tabs.length;
    // Bring Chrome to front
    chrome.activate();
    var newTab = win.tabs[win.tabs.length - 1];
    JSON.stringify({{
        index: win.tabs.length - 1,
        title: newTab.title(),
        url: newTab.url(),
        window_index: 0,
        id: stableId(newTab),
        window_id: stableId(win)
    }});
    '''
    try:
        result = _run_osascript(
            script,
            language="JavaScript",
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return AppleScriptTab(
            index=data["index"],
            title=data["title"],
            url=data["url"],
            window_index=data.get("window_index", 0),
            id=str(data.get("id") or ""),
            window_id=str(data.get("window_id") or ""),
        )
    except Exception:
        return None


def _build_navigate_tab_script(
    url: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str:
    if not isinstance(url, str):
        raise TypeError("url must be a string")
    selection = _build_tab_selection_script(
        tab_index=tab_index,
        window_index=window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return (
        'var chrome = Application("Google Chrome");\n'
        + selection
        + f"tab.url = {json.dumps(url)};\n"
        + '"ok";\n'
    )


def navigate_tab_outcome(
    url: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    """Navigate an exact Apple Events tab without collapsing delivery failures."""
    try:
        script = _build_navigate_tab_script(
            url,
            tab_index=tab_index,
            window_index=window_index,
            tab_id=tab_id,
            window_id=window_id,
        )
    except (TypeError, ValueError):
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)
    try:
        result = _run_osascript(
            script,
            language="JavaScript",
            timeout=10,
        )
        if result.returncode == 0:
            return ShadowExecutionOutcome(
                ShadowExecutionStatus.CONFIRMED,
                "Navigation dispatched.",
            )
        return ShadowExecutionOutcome(ShadowExecutionStatus.OUTCOME_UNKNOWN)
    except FileNotFoundError:
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)
    except Exception:
        return ShadowExecutionOutcome(ShadowExecutionStatus.OUTCOME_UNKNOWN)


def navigate_tab(
    url: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str:
    """Navigate an existing Chrome tab to a URL via Apple Events."""
    if not isinstance(url, str):
        return "ERROR: url must be a string"
    outcome = navigate_tab_outcome(
        url,
        tab_index=tab_index,
        window_index=window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    if outcome.status is ShadowExecutionStatus.CONFIRMED:
        return outcome.value or "Navigation dispatched."
    return "ERROR: Apple Events navigation failed"


def get_active_tab_title() -> str:
    """Get the title of Chrome's currently active tab."""
    try:
        result = _run_osascript(
            'tell application "Google Chrome" to '
            'title of active tab of front window',
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_active_tab_url() -> str:
    """Get the URL of Chrome's currently active tab."""
    try:
        result = _run_osascript(
            'tell application "Google Chrome" to URL of active tab of front window',
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


_JS_DISABLED_MSG = (
    "JavaScript from Apple Events is disabled in Chrome.\n"
    "To enable: Chrome menu bar > View > Developer > Allow JavaScript from Apple Events.\n"
    "This is a one-time setting that persists across Chrome restarts."
)


def is_js_enabled() -> bool:
    """Check if Chrome allows JavaScript execution from AppleScript."""
    result = execute_javascript("1+1")
    return result == "2"


def execute_javascript(
    js_code: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str:
    """Execute JavaScript in a Chrome tab via AppleScript. Returns the result as string."""
    if not isinstance(js_code, str):
        return "ERROR: js_code must be a string"
    try:
        selection = _build_tab_selection_script(
            tab_index=tab_index,
            window_index=window_index,
            tab_id=tab_id,
            window_id=window_id,
        )
    except (TypeError, ValueError) as exc:
        return f"ERROR: {exc}"
    script = (
        'var chrome = Application("Google Chrome");\n'
        + selection
        + f"tab.execute({{javascript: {json.dumps(js_code)}}});\n"
    )
    try:
        result = _run_osascript(
            script,
            language="JavaScript",
            timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "AppleScript" in stderr and ("JavaScript" in stderr or "turned off" in stderr):
                return f"ERROR: {_JS_DISABLED_MSG}"
            return "ERROR: Apple Events JavaScript failed"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: JavaScript execution timed out"
    except Exception:
        return "ERROR: Apple Events JavaScript failed"


def get_page_text_content(tab_index: int = 0, window_index: int = 0, max_length: int = 5000) -> str:
    """Get the visible text content of a Chrome tab's page."""
    try:
        max_length = int(_validated_number(max_length, "max_length"))
    except (TypeError, ValueError) as exc:
        return f"ERROR: {exc}"
    max_length = max(0, min(max_length, 100_000))
    js = f"""
    (function() {{
        var walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_ELEMENT,
            {{
                acceptNode: function(node) {{
                    var style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') {{
                        return NodeFilter.FILTER_REJECT;
                    }}
                    return NodeFilter.FILTER_ACCEPT;
                }}
            }}
        );
        var items = [];
        var node;
        while (node = walker.nextNode()) {{
            var role = node.getAttribute('role') || node.tagName.toLowerCase();
            var text = '';
            for (var i = 0; i < node.childNodes.length; i++) {{
                if (node.childNodes[i].nodeType === Node.TEXT_NODE) {{
                    text += node.childNodes[i].textContent.trim() + ' ';
                }}
            }}
            text = text.trim();
            if (text || ['input','select','textarea','button','a','img'].includes(node.tagName.toLowerCase())) {{
                var label = node.getAttribute('aria-label') || node.getAttribute('alt') || '';
                var val = node.value || '';
                var entry = role;
                if (text) entry += ': ' + text.substring(0, 200);
                if (label) entry += ' [' + label + ']';
                if (val) entry += ' =' + val.substring(0, 100);
                items.push(entry);
            }}
            if (items.join('\\n').length > {max_length}) break;
        }}
        return items.join('\\n');
    }})()
    """
    return execute_javascript(js, tab_index, window_index)


def get_page_accessibility_summary(tab_index: int = 0, window_index: int = 0) -> str:
    """Get a structured accessibility summary of the page — headings, links, buttons, inputs, lists."""
    js = """
    (function() {
        var result = [];

        // Headings
        var headings = document.querySelectorAll('h1,h2,h3,h4,h5,h6');
        if (headings.length) {
            result.push('HEADINGS:');
            headings.forEach(function(h) {
                result.push('  ' + h.tagName + ': ' + h.textContent.trim().substring(0, 150));
            });
        }

        // Navigation / lists
        var navs = document.querySelectorAll('nav, [role="navigation"]');
        if (navs.length) {
            result.push('NAVIGATION:');
            navs.forEach(function(n, i) {
                var label = n.getAttribute('aria-label') || ('nav-' + i);
                var links = n.querySelectorAll('a');
                result.push('  ' + label + ' (' + links.length + ' links)');
            });
        }

        // Interactive elements
        var buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
        if (buttons.length) {
            result.push('BUTTONS (' + buttons.length + '):');
            var shown = Math.min(buttons.length, 20);
            for (var i = 0; i < shown; i++) {
                var b = buttons[i];
                var text = b.textContent.trim() || b.getAttribute('aria-label') || b.getAttribute('title') || '';
                result.push('  - ' + text.substring(0, 100));
            }
            if (buttons.length > 20) result.push('  ... and ' + (buttons.length - 20) + ' more');
        }

        // Inputs
        var inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
        if (inputs.length) {
            result.push('INPUTS (' + inputs.length + '):');
            inputs.forEach(function(inp) {
                var label = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || inp.name || inp.type;
                var val = inp.value ? ' = "' + inp.value.substring(0, 50) + '"' : '';
                result.push('  - ' + inp.tagName.toLowerCase() + '[' + (inp.type || '') + '] ' + label + val);
            });
        }

        // Links
        var links = document.querySelectorAll('a[href]');
        if (links.length) {
            result.push('LINKS (' + links.length + '):');
            var shown = Math.min(links.length, 15);
            for (var i = 0; i < shown; i++) {
                var a = links[i];
                var text = a.textContent.trim() || a.getAttribute('aria-label') || '';
                result.push('  - ' + text.substring(0, 80));
            }
            if (links.length > 15) result.push('  ... and ' + (links.length - 15) + ' more');
        }

        // Chat-specific: look for chat list patterns (WhatsApp, Slack, etc.)
        var chatItems = document.querySelectorAll('[role="listitem"], [role="row"], [data-testid*="chat"], [class*="chat-list"] > div');
        if (chatItems.length) {
            result.push('CHAT/LIST ITEMS (' + chatItems.length + '):');
            var shown = Math.min(chatItems.length, 20);
            for (var i = 0; i < shown; i++) {
                var item = chatItems[i];
                var text = item.textContent.trim().replace(/\\s+/g, ' ').substring(0, 150);
                result.push('  [' + i + '] ' + text);
            }
            if (chatItems.length > 20) result.push('  ... and ' + (chatItems.length - 20) + ' more');
        }

        return result.join('\\n');
    })()
    """
    return execute_javascript(js, tab_index, window_index)


def get_process_name(pid: int) -> str:
    """Get the process name for a PID on macOS."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def is_browser_pid(pid: int) -> bool:
    """Check if a PID belongs to a Chromium-based browser."""
    name = get_process_name(pid).lower()
    browsers = ("google chrome", "chromium", "brave", "microsoft edge", "arc", "vivaldi", "opera")
    return any(b in name for b in browsers)


def get_chrome_active_tab_index() -> tuple[int, int]:
    """Get the (window_index, tab_index) of Chrome's active tab. Both 0-based."""
    script = '''
    var chrome = Application("Google Chrome");
    var win = chrome.windows[0];
    var activeIdx = win.activeTabIndex() - 1;  // AppleScript is 1-based
    JSON.stringify({window: 0, tab: activeIdx});
    '''
    try:
        result = _run_osascript(
            script,
            language="JavaScript",
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return (data["window"], data["tab"])
    except Exception:
        pass
    return (0, 0)


def shadow_execute_js(
    js_code: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str | None:
    """Execute JavaScript in a Chrome tab WITHOUT focusing Chrome.
    Works entirely in the background via AppleScript.
    Returns the JS result as a string, or None on failure.
    Note: Promises/async return empty string — use polling pattern instead.
    """
    outcome = shadow_execute_js_outcome(
        js_code,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    if outcome.status is ShadowExecutionStatus.CONFIRMED:
        return outcome.value
    return None


def shadow_execute_js_outcome(
    js_code: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    """Execute background JavaScript without collapsing uncertain delivery."""
    if sys.platform != "darwin":
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)
    try:
        script = _build_shadow_execute_script(
            js_code,
            tab_index=tab_index,
            window_index=window_index,
            tab_id=tab_id,
            window_id=window_id,
        )
    except (TypeError, ValueError):
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)

    try:
        result = _run_osascript(
            script,
            language="JavaScript",
            timeout=10,
        )
        if result.returncode == 0:
            return ShadowExecutionOutcome(
                ShadowExecutionStatus.CONFIRMED,
                result.stdout.strip(),
            )
        return ShadowExecutionOutcome(ShadowExecutionStatus.OUTCOME_UNKNOWN)
    except Exception:
        return ShadowExecutionOutcome(ShadowExecutionStatus.OUTCOME_UNKNOWN)


def _shadow_execute_targeted(
    js_code: str,
    tab_index: int,
    window_index: int,
    *,
    tab_id: str,
    window_id: str,
) -> str | None:
    if tab_id or window_id:
        return shadow_execute_js(
            js_code,
            tab_index,
            window_index,
            tab_id=tab_id,
            window_id=window_id,
        )
    return shadow_execute_js(js_code, tab_index, window_index)


def _shadow_execute_targeted_outcome(
    js_code: str,
    tab_index: int,
    window_index: int,
    *,
    tab_id: str,
    window_id: str,
) -> ShadowExecutionOutcome:
    return shadow_execute_js_outcome(
        js_code,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_click_script(selector: str) -> str:
    return (
        f"var selector = {json.dumps(selector)};"
        'var el = document.querySelector(selector);'
        'el ? (el.click(), "clicked") : "not found"'
    )


def shadow_click(
    selector: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> bool:
    """Click an element by CSS selector in background."""
    result = _shadow_execute_targeted(
        _shadow_click_script(selector),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return result == "clicked"


def shadow_click_outcome(
    selector: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    return _shadow_execute_targeted_outcome(
        _shadow_click_script(selector),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_click_by_text_script(text: str, role: str = "") -> str:
    return (
        f"var expectedText = {json.dumps(text)};"
        f"var expectedRole = {json.dumps(role)};"
        'var els = Array.from(document.querySelectorAll("button, a, [role=button], [role=link], [role=menuitem], [role=tab], [role=option]"));'
        'if(expectedRole){ els = els.filter(e => e.getAttribute("role") === expectedRole); }'
        'var el = els.find(e => e.textContent.trim().includes(expectedText));'
        f'el ? (el.click(), el.tagName + ": " + el.textContent.trim().substring(0,50)) : "not found"'
    )


def shadow_click_by_text(
    text: str,
    role: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str | None:
    """Click an element by its text content in background. Returns element info or None."""
    result = _shadow_execute_targeted(
        _shadow_click_by_text_script(text, role),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return result if result and result != "not found" else None


def shadow_click_by_text_outcome(
    text: str,
    role: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    return _shadow_execute_targeted_outcome(
        _shadow_click_by_text_script(text, role),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_type_script(text: str, selector: str = "") -> str:
    target = (
        f"var selector = {json.dumps(selector)};"
        "var el = document.querySelector(selector);"
        if selector
        else "var el = document.activeElement;"
    )
    return (
        "(function(){"
        f"var text = {json.dumps(text)};"
        f"{target}"
        'if(!el){ return "not found"; }'
        'var before = ("value" in el) ? String(el.value || "") : String(el.textContent || "");'
        "el.focus();"
        'var acknowledged = document.execCommand("insertText", false, text) === true;'
        'var after = ("value" in el) ? String(el.value || "") : String(el.textContent || "");'
        'return acknowledged && after !== before ? "typed" : "rejected";'
        "})()"
    )


def shadow_type(
    text: str,
    selector: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> bool:
    """Type text into a web element in background using execCommand.
    If no selector, types into the currently focused element.
    """
    result = _shadow_execute_targeted(
        _shadow_type_script(text, selector),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return result == "typed"


def shadow_type_outcome(
    text: str,
    selector: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    return _shadow_execute_targeted_outcome(
        _shadow_type_script(text, selector),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_press_key_script(key: str) -> str | None:
    key_map = {
        "enter": ("Enter", "Enter", 13),
        "tab": ("Tab", "Tab", 9),
        "escape": ("Escape", "Escape", 27),
        "backspace": ("Backspace", "Backspace", 8),
        "delete": ("Delete", "Delete", 46),
        "arrowup": ("ArrowUp", "ArrowUp", 38),
        "arrowdown": ("ArrowDown", "ArrowDown", 40),
        "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
        "arrowright": ("ArrowRight", "ArrowRight", 39),
        "space": (" ", "Space", 32),
    }
    if not isinstance(key, str):
        return None
    k = key_map.get(key.lower(), (key, f"Key{key.upper()}", 0))
    event_data = json.dumps({"key": k[0], "code": k[1], "keyCode": k[2], "which": k[2]})
    return (
        f'var el = document.activeElement || document.body;'
        f'["keydown","keypress","keyup"].forEach(t => el.dispatchEvent(new KeyboardEvent(t, '
        f'Object.assign({event_data}, {{bubbles:true}}))));'
        f'"pressed"'
    )


def shadow_press_key(
    key: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> bool:
    """Dispatch a keyboard event in background."""
    js = _shadow_press_key_script(key)
    if js is None:
        return False
    result = _shadow_execute_targeted(
        js,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return result == "pressed"


def shadow_press_key_outcome(
    key: str,
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    js = _shadow_press_key_script(key)
    if js is None:
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)
    return _shadow_execute_targeted_outcome(
        js,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_scroll_script(
    direction: str = "down",
    amount: int = 300,
    selector: str = "",
) -> str | None:
    if direction not in {"up", "down"}:
        return None
    try:
        amount = _validated_number(amount, "amount")
    except (TypeError, ValueError):
        return None
    dy = amount if direction == "down" else -amount
    if selector:
        return (
            f"var selector={json.dumps(selector)};"
            f"var dy={json.dumps(dy)};"
            'var el=document.querySelector(selector);'
            'el?(el.scrollBy(0,dy),"scrolled"):"not found"'
        )
    return f'window.scrollBy(0,{json.dumps(dy)}); "scrolled"'


def shadow_scroll(
    direction: str = "down",
    amount: int = 300,
    selector: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> bool:
    """Scroll page or element in background."""
    js = _shadow_scroll_script(direction, amount, selector)
    if js is None:
        return False
    result = _shadow_execute_targeted(
        js,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )
    return result == "scrolled"


def shadow_scroll_outcome(
    direction: str = "down",
    amount: int = 300,
    selector: str = "",
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> ShadowExecutionOutcome:
    js = _shadow_scroll_script(direction, amount, selector)
    if js is None:
        return ShadowExecutionOutcome(ShadowExecutionStatus.NOT_DISPATCHED)
    return _shadow_execute_targeted_outcome(
        js,
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def _shadow_read_interactive_script() -> str:
    """Build a bounded interactive-element scan that never reads secure values."""
    return (
        'Array.from(document.querySelectorAll("button, a, input, textarea, select, '
        '[role=button], [role=link], [role=menuitem], [role=tab], [role=checkbox], '
        '[role=textbox], [role=combobox], [contenteditable=true]"))'
        '.filter(e => e.offsetParent !== null)'
        '.slice(0, 50)'
        '.map((e, i) => {'
        '  var r = e.getBoundingClientRect();'
        '  var tag = e.tagName.toLowerCase();'
        '  var role = e.getAttribute("role") || tag;'
        '  var type = String(e.getAttribute("type") || e.type || "").toLowerCase();'
        '  var autocomplete = String(e.getAttribute("autocomplete") || "").toLowerCase();'
        '  var secure = type === "password" || autocomplete.includes("current-password") || autocomplete.includes("new-password");'
        '  var label = e.getAttribute("aria-label") || e.placeholder || "";'
        '  if (!secure) { label = label || e.textContent || e.value || ""; }'
        '  var text = String(label).trim().substring(0, 60);'
        '  return "[" + i + "] " + role + " \\"" + text + "\\" @(" + Math.round(r.x) + "," + Math.round(r.y) + ") " + Math.round(r.width) + "x" + Math.round(r.height);'
        '}).join("\\n")'
    )


def shadow_read_interactive(
    tab_index: int = 0,
    window_index: int = 0,
    *,
    tab_id: str = "",
    window_id: str = "",
) -> str | None:
    """Read all interactive elements with positions from a Chrome tab in background."""
    return _shadow_execute_targeted(
        _shadow_read_interactive_script(),
        tab_index,
        window_index,
        tab_id=tab_id,
        window_id=window_id,
    )


def shadow_get_active_tab_index(window_index: int = 0) -> int | None:
    """Get the active tab index (0-based) of a Chrome window."""
    if sys.platform != "darwin":
        return None
    try:
        window_index = _validated_index(window_index, "window_index")
        result = _run_osascript(
            'tell application "Google Chrome" to get active tab index of '
            f"window {window_index + 1}",
            timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip()) - 1  # Convert to 0-based
        return None
    except Exception:
        return None
