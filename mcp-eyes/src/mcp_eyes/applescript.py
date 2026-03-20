"""macOS AppleScript helpers for Chrome browser interaction.

Provides fallback access to Chrome tabs and web content WITHOUT requiring
--remote-debugging-port=9222. Uses macOS native AppleScript/JXA via osascript.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class AppleScriptTab:
    """A Chrome tab discovered via AppleScript."""
    index: int       # 0-based index
    title: str
    url: str
    window_index: int = 0


def is_available() -> bool:
    """Check if AppleScript Chrome access is available (macOS + Chrome running)."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "Google Chrome"'],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def list_chrome_tabs() -> list[AppleScriptTab]:
    """List all Chrome tabs using AppleScript. No remote debugging needed."""
    script = '''
    tell application "Google Chrome"
        set tabData to {}
        set winIdx to 0
        repeat with w in windows
            set tabIdx to 0
            repeat with t in tabs of w
                set end of tabData to {winIdx, tabIdx, title of t, URL of t}
                set tabIdx to tabIdx + 1
            end repeat
            set winIdx to winIdx + 1
        end repeat
        return tabData
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        return _parse_tab_list(result.stdout.strip())
    except Exception:
        return []


def _parse_tab_list(raw: str) -> list[AppleScriptTab]:
    """Parse AppleScript tab list output into structured data."""
    if not raw:
        return []

    tabs = []
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
    chrome.windows().forEach(function(win, winIdx) {
        win.tabs().forEach(function(tab, tabIdx) {
            tabs.push({
                window_index: winIdx,
                index: tabIdx,
                title: tab.title(),
                url: tab.url()
            });
        });
    });
    JSON.stringify(tabs);
    '''
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, timeout=10,
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
        )
        for item in data
    ]


def _list_tabs_individual() -> list[AppleScriptTab]:
    """Fallback: query tabs one window at a time."""
    # Get window count
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to count of windows'],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return []

    win_count = int(result.stdout.strip())
    tabs = []
    global_idx = 0

    for win_idx in range(1, win_count + 1):
        script = f'''
        tell application "Google Chrome"
            set w to window {win_idx}
            set tabCount to count of tabs of w
            set output to ""
            repeat with i from 1 to tabCount
                set t to tab i of w
                set output to output & title of t & "\\n" & URL of t & "\\n---\\n"
            end repeat
            return output
        end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            continue

        entries = result.stdout.strip().split("---\n")
        for entry in entries:
            lines = entry.strip().split("\n")
            if len(lines) >= 2:
                tabs.append(AppleScriptTab(
                    index=global_idx,
                    title=lines[0].strip(),
                    url=lines[1].strip(),
                    window_index=win_idx - 1,
                ))
                global_idx += 1

    return tabs


def get_active_tab_title() -> str:
    """Get the title of Chrome's currently active tab."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to title of active tab of front window'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_active_tab_url() -> str:
    """Get the URL of Chrome's currently active tab."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to URL of active tab of front window'],
            capture_output=True, text=True, timeout=5,
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


def execute_javascript(js_code: str, tab_index: int = 0, window_index: int = 0) -> str:
    """Execute JavaScript in a Chrome tab via AppleScript. Returns the result as string."""
    # Use JXA for reliable escaping
    script = f'''
    var chrome = Application("Google Chrome");
    var win = chrome.windows[{window_index}];
    var tab = win.tabs[{tab_index}];
    tab.execute({{javascript: {json.dumps(js_code)}}});
    '''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "AppleScript" in stderr and ("JavaScript" in stderr or "turned off" in stderr):
                return f"ERROR: {_JS_DISABLED_MSG}"
            return f"ERROR: {stderr}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: JavaScript execution timed out"
    except Exception as e:
        return f"ERROR: {e}"


def get_page_text_content(tab_index: int = 0, window_index: int = 0, max_length: int = 5000) -> str:
    """Get the visible text content of a Chrome tab's page."""
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
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return (data["window"], data["tab"])
    except Exception:
        pass
    return (0, 0)
