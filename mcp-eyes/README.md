# agent-eyes

Accessibility-tree vision for AI agents — see and interact with **any application** without screenshots.

agent-eyes gives AI agents structured, token-efficient access to every UI element on screen. Instead of sending costly screenshots to vision models, it reads the operating system's accessibility tree and returns a numbered element list that any LLM can understand and act on.

## Why accessibility over screenshots?

| | Screenshots | agent-eyes |
|---|---|---|
| **Token cost** | 10,000-50,000 tokens/image | 200-2,000 tokens/tree |
| **Interaction** | "Click at pixel (453, 287)" | `eyes_click(id=42)` |
| **Reliability** | Resolution/theme dependent | Semantic, theme-independent |
| **Speed** | 1-3s capture + vision model | 50-200ms tree traversal |
| **Background** | Requires visible window | Shadow mode works unfocused |

## Quick start

```bash
# Install
uv pip install -e ".[macos]"

# Run as MCP server
agent-eyes
```

Add to your MCP client config (e.g., Claude Code `settings.json`):

```json
{
  "mcpServers": {
    "agent-eyes": {
      "command": "agent-eyes"
    }
  }
}
```

## Platform support

| Platform | Accessibility API | Input Simulation | Status |
|----------|------------------|-----------------|--------|
| **macOS** | AXUIElement (PyObjC) | CGEvent (Quartz) | Full support |
| **Windows** | UI Automation (pywinauto) | SendInput (ctypes) | Core support |
| **Linux** | AT-SPI2 (GObject) | xdotool / ydotool | Core support |

## 28 tools

### Core — See and interact

| Tool | What it does |
|------|-------------|
| `eyes_status` | Check platform, permissions, and CDP availability |
| `eyes_list_apps` | List all running apps with PIDs and window titles |
| `eyes_get_tree` | Get the full accessibility tree for any app by PID |
| `eyes_get_subtree` | Drill into a specific element's children (token-efficient) |
| `eyes_find` | Search elements by role, name, value with fuzzy matching |
| `eyes_context` | One-call snapshot: frontmost app + window + interactive elements |
| `eyes_get_focused` | Get the currently focused element |
| `eyes_element_at` | Identify the element at specific screen coordinates |

### Interact — Click, type, press, scroll

| Tool | What it does |
|------|-------------|
| `eyes_click` | Click by element ID or screen coordinates (x, y) |
| `eyes_type` | Type text with human-like keyboard simulation |
| `eyes_press_key` | Press keyboard keys (Enter, Tab, Escape, shortcuts) |
| `eyes_scroll` | Scroll native apps or web pages |
| `eyes_hover` | Hover to trigger tooltips and dropdown previews |
| `eyes_drag` | Drag elements (web/CDP) |
| `eyes_fill_form` | Fill multiple form fields at once (web/CDP) |

### App and window management

| Tool | What it does |
|------|-------------|
| `eyes_app` | Launch, quit, or focus any application |
| `eyes_window` | List, focus, minimize, close, move, or resize windows |

### Chrome / web

| Tool | What it does |
|------|-------------|
| `eyes_list_chrome_tabs` | List all Chrome tabs |
| `eyes_get_web_tree` | Get web accessibility tree via CDP |
| `eyes_navigate` | Navigate a Chrome tab to a URL |
| `eyes_new_tab` | Open a new Chrome tab |
| `eyes_close_tab` | Close a Chrome tab |
| `eyes_evaluate` | Execute JavaScript in a Chrome tab |
| `eyes_handle_dialog` | Accept/dismiss browser dialogs |
| `eyes_file_upload` | Upload files to web forms |
| `eyes_wait_for` | Wait for an element to appear (native or web) |

### Shadow mode — background browser control

| Tool | What it does |
|------|-------------|
| `eyes_shadow` | Full browser interaction WITHOUT focusing Chrome |
| `eyes_get_ocr_hints` | Visual text detection via native OCR (fallback) |

Shadow mode supports: `click`, `type`, `press_key`, `scroll`, `read`, and raw `js` execution — all running in the background via AppleScript JS injection. Chrome stays unfocused.

## Key features

### Universal app interaction

Works with **any** application — native macOS, Electron, CEF, WebKit, Qt:

- Automatically detects web content (`AXWebArea`) and extends tree depth
- Calls `AXEnhancedUserInterface` on all apps to force accessibility tree construction
- Dynamic element cap: 1,000 for native apps, 3,000 for web content
- Auto-retry with deeper depth when web content is sparse

### Smart type strategy

Three-layer approach that handles every app correctly:

| App type | Strategy | Why |
|----------|----------|-----|
| Web apps (React, Vue) | Keyboard injection, no verification | AXValue updates async; set_value breaks framework state |
| Native apps | Keyboard injection + verification | Falls back to set_value if CGEvent didn't land |
| Secure text fields (Jamf, password dialogs) | set_value directly | CGEvent blocked by `EnableSecureEventInput` |

### OCR vision fallback

When the accessibility tree is insufficient, `eyes_get_ocr_hints` captures a window screenshot and runs native OCR:

- **macOS**: Apple Vision framework (`VNRecognizeTextRequest`)
- **Windows**: `Windows.Media.Ocr`
- **Linux**: Tesseract via pytesseract

Returns text blocks with screen coordinates for coordinate-based clicking.

### Fuzzy element search

`eyes_find` supports 5 match types: `contains` (default), `exact`, `regex`, `prefix`, `suffix`.

### Crash safety

- Stale element validation before actions (prevents SIGSEGV on destroyed elements)
- Try/except around tree traversal children (app crash doesn't kill the server)
- 30-second TTL on Chrome tab cache (prevents stale tab index errors)
- Full traceback logging on tool errors

## Requirements

### macOS

- **Accessibility permission**: System Settings > Privacy & Security > Accessibility
- **Screen Recording** (optional, for OCR): System Settings > Privacy & Security > Screen Recording
- Python 3.10+, PyObjC frameworks

### Windows

- Python 3.10+, pywinauto
- No special permissions needed (admin may be required for UAC-elevated apps)

### Linux

- Python 3.10+, at-spi2-core, python3-pyatspi
- AT-SPI2 daemon must be running

### Chrome web features

For CDP-based tools (`eyes_get_web_tree`, `eyes_evaluate`, `eyes_fill_form`):

```bash
# Launch Chrome with remote debugging
open -a 'Google Chrome' --args --remote-debugging-port=9222
```

Shadow mode (`eyes_shadow`) and native tree tools work **without** CDP.

## Architecture

```
┌──────────────────────────────────────────────┐
│                  MCP Server                  │
│              (server.py — 28 tools)          │
├──────────┬──────────┬──────────┬─────────────┤
│  Native  │   CDP    │  Input   │   Shadow    │
│ Adapters │  Client  │   Sim    │    Mode     │
├──────────┼──────────┼──────────┼─────────────┤
│ macOS AX │ Chrome   │ CGEvent  │ AppleScript │
│ Win UIA  │ DevTools │ SendInput│ JS inject   │
│ Linux    │ Protocol │ xdotool  │             │
│ AT-SPI2  │          │          │             │
└──────────┴──────────┴──────────┴─────────────┘
```

## License

MIT
