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
        for tab_entry_idx, entry in enumerate(entries):
            lines = entry.strip().split("\n")
            if len(lines) >= 2:
                tabs.append(AppleScriptTab(
                    index=tab_entry_idx,
                    title=lines[0].strip(),
                    url=lines[1].strip(),
                    window_index=win_idx - 1,
                ))

    return tabs


def open_new_tab(url: str = "about:blank") -> AppleScriptTab | None:
    """Open a new tab in Chrome via AppleScript. No remote debugging needed."""
    script = f'''
    var chrome = Application("Google Chrome");
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
    // Wait briefly for the tab to start loading
    delay(0.5);
    var newTab = win.tabs[win.tabs.length - 1];
    JSON.stringify({{
        index: win.tabs.length - 1,
        title: newTab.title(),
        url: newTab.url(),
        window_index: 0
    }});
    '''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return AppleScriptTab(
            index=data["index"],
            title=data["title"],
            url=data["url"],
            window_index=data.get("window_index", 0),
        )
    except Exception:
        return None


def navigate_tab(url: str, tab_index: int = 0, window_index: int = 0) -> str:
    """Navigate an existing Chrome tab to a URL via AppleScript."""
    script = f'''
    var chrome = Application("Google Chrome");
    var win = chrome.windows[{window_index}];
    var tab = win.tabs[{tab_index}];
    tab.url = {json.dumps(url)};
    chrome.activate();
    delay(0.5);
    JSON.stringify({{
        title: tab.title(),
        url: tab.url()
    }});
    '''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return f"ERROR: {result.stderr.strip()}"
        data = json.loads(result.stdout.strip())
        return f"Navigated to: {data['url']}\nTitle: {data['title']}"
    except Exception as e:
        return f"ERROR: {e}"


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


def shadow_execute_js(js_code: str, tab_index: int = 0, window_index: int = 0) -> str | None:
    """Execute JavaScript in a Chrome tab WITHOUT focusing Chrome.
    Works entirely in the background via AppleScript.
    Returns the JS result as a string, or None on failure.
    Note: Promises/async return empty string — use polling pattern instead.
    """
    if sys.platform != "darwin":
        return None
    # Escape backslashes and quotes for AppleScript string
    escaped = js_code.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "Google Chrome" to tell tab {tab_index + 1} '
        f'of window {window_index + 1} to execute javascript "{escaped}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, Exception):
        return None


def shadow_click(selector: str, tab_index: int = 0, window_index: int = 0) -> bool:
    """Click an element by CSS selector in background."""
    js = f'var el = document.querySelector("{selector}"); el ? (el.click(), "clicked") : "not found"'
    result = shadow_execute_js(js, tab_index, window_index)
    return result == "clicked"


def shadow_click_by_text(text: str, role: str = "", tab_index: int = 0, window_index: int = 0) -> str | None:
    """Click an element by its text content in background. Returns element info or None."""
    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
    role_selector = f'[role=\\"{role}\\"], ' if role else ''
    js = (
        f'var els = Array.from(document.querySelectorAll("{role_selector}button, a, [role=button], [role=link], [role=menuitem], [role=tab], [role=option]"));'
        f'var el = els.find(e => e.textContent.trim().includes("{escaped_text}"));'
        f'el ? (el.click(), el.tagName + ": " + el.textContent.trim().substring(0,50)) : "not found"'
    )
    result = shadow_execute_js(js, tab_index, window_index)
    return result if result and result != "not found" else None


def shadow_type(text: str, selector: str = "", tab_index: int = 0, window_index: int = 0) -> bool:
    """Type text into a web element in background using execCommand.
    If no selector, types into the currently focused element.
    """
    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if selector:
        escaped_sel = selector.replace("\\", "\\\\").replace('"', '\\"')
        js = (
            f'var el = document.querySelector("{escaped_sel}");'
            f'if(el){{ el.focus(); document.execCommand("insertText", false, "{escaped_text}"); "typed" }}'
            f'else{{ "not found" }}'
        )
    else:
        js = f'document.execCommand("insertText", false, "{escaped_text}"); "typed"'
    result = shadow_execute_js(js, tab_index, window_index)
    return result == "typed"


def shadow_press_key(key: str, tab_index: int = 0, window_index: int = 0) -> bool:
    """Dispatch a keyboard event in background."""
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
    k = key_map.get(key.lower(), (key, f"Key{key.upper()}", 0))
    js = (
        f'var el = document.activeElement || document.body;'
        f'["keydown","keypress","keyup"].forEach(t => el.dispatchEvent(new KeyboardEvent(t, '
        f'{{key:"{k[0]}", code:"{k[1]}", keyCode:{k[2]}, which:{k[2]}, bubbles:true}})));'
        f'"pressed"'
    )
    result = shadow_execute_js(js, tab_index, window_index)
    return result == "pressed"


def shadow_scroll(direction: str = "down", amount: int = 300, selector: str = "",
                  tab_index: int = 0, window_index: int = 0) -> bool:
    """Scroll page or element in background."""
    if selector:
        escaped_sel = selector.replace("\\", "\\\\").replace('"', '\\"')
        dy = amount if direction == "down" else -amount
        js = f'var el=document.querySelector("{escaped_sel}"); el?(el.scrollBy(0,{dy}),"scrolled"):"not found"'
    else:
        dy = amount if direction == "down" else -amount
        js = f'window.scrollBy(0,{dy}); "scrolled"'
    result = shadow_execute_js(js, tab_index, window_index)
    return result == "scrolled"


def shadow_read_interactive(tab_index: int = 0, window_index: int = 0) -> str | None:
    """Read all interactive elements with positions from a Chrome tab in background."""
    js = (
        'Array.from(document.querySelectorAll("button, a, input, textarea, select, '
        '[role=button], [role=link], [role=menuitem], [role=tab], [role=checkbox], '
        '[role=textbox], [role=combobox], [contenteditable=true]"))'
        '.filter(e => e.offsetParent !== null)'  # visible only
        '.slice(0, 50)'
        '.map((e, i) => {'
        '  var r = e.getBoundingClientRect();'
        '  var text = (e.textContent || e.value || e.placeholder || e.ariaLabel || "").trim().substring(0, 60);'
        '  var tag = e.tagName.toLowerCase();'
        '  var role = e.getAttribute("role") || tag;'
        '  var type = e.type || "";'
        '  return "[" + i + "] " + role + " \\"" + text + "\\" @(" + Math.round(r.x) + "," + Math.round(r.y) + ") " + Math.round(r.width) + "x" + Math.round(r.height);'
        '}).join("\\n")'
    )
    return shadow_execute_js(js, tab_index, window_index)


def shadow_get_active_tab_index(window_index: int = 0) -> int | None:
    """Get the active tab index (0-based) of a Chrome window."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", f'tell application "Google Chrome" to get active tab index of window {window_index + 1}'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip()) - 1  # Convert to 0-based
        return None
    except Exception:
        return None
