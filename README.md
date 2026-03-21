# agent-eyes

Accessibility-tree vision for AI agents — see and interact with **any** application without screenshots.

Instead of pixel-based screen capture, agent-eyes reads the OS accessibility tree to give AI agents a structured, semantic view of every UI element on screen. The tree **is** the vision.

## Key Advantages

- **No screenshots needed** — works through accessibility APIs, not pixels
- **Cross-platform** — macOS (AXUIElement), Windows (UI Automation), Linux (AT-SPI2)
- **Native + Web** — interact with desktop apps and Chrome tabs from one server
- **Shadow mode** — control Chrome in the background without stealing window focus
- **Human-like input** — real keyboard/mouse events that trigger all event listeners
- **Element IDs** — every UI element gets an `[id]` for precise click/type targeting
- **OCR fallback** — for apps with sparse accessibility trees, get text via screen OCR

## Installation

Run directly via `uvx` (no install needed):

```bash
uvx agent-eyes
```

### Linux only — install AT-SPI2 via system package manager

```bash
apt install python3-pyatspi   # Debian/Ubuntu
dnf install python3-pyatspi   # Fedora
```

**Requirements:** Python 3.10+ &bull; Chrome with `--remote-debugging-port=9222` for web tools

## Quick Start

### As an MCP server

Add to your Claude Code config (`~/.claude.json`):

```json
{
  "mcpServers": {
    "agent-eyes": {
      "command": "uvx",
      "args": ["agent-eyes"]
    }
  }
}
```

### Standalone

```bash
agent-eyes
```

## First-Time Setup

After adding agent-eyes as an MCP server, run the setup wizard to auto-detect competing servers (Playwright, Puppeteer, etc.) and configure your AI tools:

```
agent-eyes:init
```

This scans your machine for AI coding tools and competing MCP servers, then presents interactive choices to replace them with agent-eyes. All changes are backed up automatically.

> **Tip:** In Claude Code, setup uses native multi-choice prompts. In other AI tools, it falls back to text-based selection.

## Tools (28)

### Orientation

| Tool | Description |
|------|-------------|
| `eyes_status` | Check platform adapter, permissions, CDP availability |
| `eyes_context` | Quick snapshot — frontmost app, active window, focused element |
| `eyes_list_apps` | List all running apps with PIDs and window titles |
| `eyes_get_focused` | Get the currently focused UI element |

### Reading UI

| Tool | Description |
|------|-------------|
| `eyes_get_tree` | Full accessibility tree of an app by PID |
| `eyes_get_subtree` | Drill into a specific subtree by element ID |
| `eyes_find` | Search elements by role, name, or value (regex/contains/exact) |
| `eyes_element_at` | Identify the element at screen coordinates |
| `eyes_get_ocr_hints` | OCR fallback — get text blocks with coordinates |

### Interaction

| Tool | Description |
|------|-------------|
| `eyes_click` | Click an element by ID or screen coordinates |
| `eyes_type` | Type text into a field with real key events |
| `eyes_press_key` | Press keys with modifiers (Enter, Tab, Ctrl+C, etc.) |
| `eyes_hover` | Hover to trigger tooltips and :hover states |
| `eyes_scroll` | Scroll vertically/horizontally in apps or browser |
| `eyes_drag` | Drag and drop between coordinates |
| `eyes_fill_form` | Fill multiple form fields in one call |
| `eyes_file_upload` | Upload files to a file input element |
| `eyes_wait_for` | Poll until an element appears (with timeout) |

### App Management

| Tool | Description |
|------|-------------|
| `eyes_app` | Launch, quit, or focus an application |
| `eyes_window` | List, focus, minimize, close, move, or resize windows |

### Chrome / Web

| Tool | Description |
|------|-------------|
| `eyes_list_chrome_tabs` | List all Chrome tabs (title, URL) |
| `eyes_get_web_tree` | Chrome tab accessibility tree via CDP |
| `eyes_navigate` | Navigate a tab to a URL |
| `eyes_evaluate` | Execute JavaScript in a tab |
| `eyes_new_tab` | Open a new Chrome tab |
| `eyes_close_tab` | Close a Chrome tab |
| `eyes_handle_dialog` | Accept/dismiss JS dialogs (alert, confirm, prompt) |

### Shadow Mode

| Tool | Description |
|------|-------------|
| `eyes_shadow` | Control Chrome **without focusing it** — click, type, scroll, read, run JS |

## How It Works

```
AI Agent
  ↓ MCP
agent-eyes server
  ├── Native Adapter (macOS / Windows / Linux)
  │     └── OS Accessibility API → structured UI tree
  ├── CDP Client (Chrome DevTools Protocol)
  │     └── Chrome tabs → web accessibility tree + JS execution
  └── Input Simulator
        └── Real keyboard/mouse events → human-like interaction
```

1. **Read** — `eyes_get_tree` returns every button, text field, heading, link, etc. as a numbered tree
2. **Find** — `eyes_find` searches by role/name/value, or `eyes_element_at` for coordinate lookup
3. **Act** — `eyes_click`, `eyes_type`, `eyes_press_key` target elements by their `[id]`

## Supported Platforms

| Platform | Native Adapter | Web (Chrome) | Shadow Mode |
|----------|---------------|-------------|-------------|
| macOS | AXUIElement + pyobjc | CDP + AppleScript fallback | Yes |
| Windows | UI Automation + pywinauto | CDP | Yes |
| Linux | AT-SPI2 + pyatspi | CDP | Yes |

## License

MIT
