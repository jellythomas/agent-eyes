---
name: init
description: >
  Interactive setup wizard for agent-eyes. Checks install state first
  (auto-triggers /agent-eyes:install if needed). Then performs a deep 3-layer
  scan: (1) MCP server configs across all AI tools (Claude Code, Desktop,
  Cursor, VS Code, Windsurf, Zed, Cline, Roo Code, Continue.dev),
  (2) skills/agents/commands/rules for competitor references,
  (3) plugins/marketplace for installed competitor packages.
  Detects 30+ competing browser, desktop, vision, and scraping automation
  MCP servers. Lets the user choose what to configure, replace, and migrate
  via multiple-choice prompts.
  Use when: agent-eyes status says "not configured yet" or "has been upgraded",
  user types /agent-eyes:init, user asks to set up or configure agent-eyes,
  user wants to add agent-eyes to their AI tools, or user asks about
  replacing competing automation MCP servers with agent-eyes.
  Also triggers on: "setup agent-eyes", "configure agent-eyes",
  "init agent-eyes", "agent-eyes first run".
user_invocable: true
---

# agent-eyes:init — Interactive Setup

## Pre-flight: Check Install State

Before proceeding, check `~/.agent-eyes/install.json`:

```bash
cat ~/.agent-eyes/install.json 2>/dev/null || echo "NOT_FOUND"
```

- If `NOT_FOUND` or `installed` is false:
  → Tell user: "Dependencies not installed. Running /agent-eyes:install first..."
  → Invoke `/agent-eyes:install` skill
  → Then continue with init below

- If file exists and `installed` is true:
  → Continue with setup wizard

---

## Setup Wizard

This skill runs the agent-eyes setup wizard with a **3-layer deep scan**:

1. **MCP layer** — detects AI tools and competing MCP servers in config files
2. **Content layer** — detects competitor references in skills, agents, commands,
   rules, and instructions across ALL AI tools
3. **Plugin layer** — detects competitor packages in plugins/marketplace directories

The whole flow is idempotent — safe to run on first install, after upgrades,
or anytime the user wants to reconfigure.

---

## Competitor Registry

agent-eyes replaces ALL of the following. The scan MUST detect every one of
these across all locations. Grouped by category:

### Browser Automation MCP Servers

| Competitor | Package / Config Key | Tool Prefix |
|-----------|---------------------|-------------|
| Playwright (Microsoft) | `@playwright/mcp` / `"playwright"` | `browser_*` |
| Playwright (Community) | `@executeautomation/playwright-mcp-server` / `"playwright-mcp"` | `browser_*` |
| Puppeteer (Official) | `@modelcontextprotocol/server-puppeteer` / `"puppeteer"` | `puppeteer_*` |
| Selenium | `@angiejones/mcp-selenium` / `"selenium"` | `selenium_*` |
| Browserbase + Stagehand | `@browserbasehq/mcp-stagehand` / `"browserbase"` | `stagehand_*` |
| BrowserMCP (Browserless) | `@browsermcp/mcp` / `"browsermcp"` | `browser_*` |
| Hyperbrowser | `hyperbrowser-mcp` / `"hyperbrowser"` | `hyperbrowser_*` |

### Desktop / OS Automation MCP Servers

| Competitor | Package / Config Key |
|-----------|---------------------|
| macOS Automator | `@steipete/macos-automator-mcp` / `"macos-automator"` |
| AppleScript | `applescript-mcp` / `"applescript"` |
| macOS UI Automation | `macos-ui-automation-mcp` / `"macos_ui_automation"` |
| AutoMac | `automac-mcp` / `"automac"` |
| PyAutoGUI | `mcp-pyautogui-server` / `"pyautogui"` |
| Windows Desktop | `mcp-windows-desktop-automation` / `"windows-desktop-automation"` |
| ScreenHand | `screenhand` / `"screenhand"` |

### Vision / Screenshot-Based MCP Servers

| Competitor | Package / Config Key |
|-----------|---------------------|
| Screenpipe | `screenpipe-mcp` / `"screenpipe"` |
| OmniMCP | `omnimcp` / `"omnimcp"` |
| MCP Screenshot | `mcp-screenshot-server` / `"screenshot"` |

---

## AI Tool Scan Locations

### Claude Code

| Type | Global | Project |
|------|--------|---------|
| MCP config | `~/.claude.json` | `.mcp.json` |
| Settings | `~/.claude/settings.json` | `.claude/settings.json` |
| Skills | `~/.claude/skills/` | `.claude/skills/` |
| Commands | `~/.claude/commands/` | `.claude/commands/` |
| Instructions | `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| Plugins | `~/.claude/plugins/` | — |

### Claude Desktop

| Type | Location (macOS) |
|------|-----------------|
| MCP config | `~/Library/Application Support/Claude/claude_desktop_config.json` |

### Cursor

| Type | Global | Project |
|------|--------|---------|
| MCP config | `~/.cursor/mcp.json` | `.cursor/mcp.json` |
| Rules | `~/.cursor/rules/` | `.cursor/rules/`, `.cursorrules` |

### VS Code (GitHub Copilot)

| Type | Project |
|------|---------|
| MCP config | `.vscode/mcp.json` |
| Instructions | `.github/copilot-instructions.md` |

### Windsurf

| Type | Global | Project |
|------|--------|---------|
| MCP config | `~/.codeium/windsurf/mcp_config.json` | — |
| Rules | `~/.codeium/windsurf/rules/` | `.windsurfrules` |

### Zed

| Type | Global |
|------|--------|
| Config | `~/.config/zed/settings.json` |

### Cline / Roo Code

| Type | Location (macOS) |
|------|-----------------|
| Cline | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Roo Code | `~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/cline_mcp_settings.json` |

### Continue.dev

| Type | Global |
|------|--------|
| Config | `~/.continue/config.yaml` or `~/.continue/config.json` |

---

## Setup Flow

### Step 1: MCP Config Scan

Read each config file listed above. For each, check if `mcpServers` (or `context_servers` for Zed) contains any competitor keys from the registry.

Build a report:
```
AI Tools Detected:
  ✓ Claude Code (configured)
  ✓ Cursor (configured)
  ✗ VS Code (not configured)

Competitors Found:
  ⚠ Playwright MCP in Claude Code (~/.claude.json)
  ⚠ macOS Automator in Cursor (~/.cursor/mcp.json)
```

### Step 2: Content Scan

Grep all skill/rule/instruction locations for competitor references:

```bash
grep -rl "playwright\|puppeteer\|selenium\|stagehand\|browserbase\|browsermcp\|screenpipe\|omnimcp\|pyautogui\|screenhand" ~/.claude/skills/ ~/.claude/commands/ ~/.cursor/rules/ 2>/dev/null
```

### Step 3: Present Choices

Use AskUserQuestion with multiple-choice options:

**Q1 — AI Tools:** "Which AI tools should agent-eyes be added to?"
**Q2 — Competitors:** "Replace competing servers?" (only if found)
**Q3 — Scope:** "Global or project-only?"
**Q4 — Migrate:** "Migrate competitor references?" (only if found)

### Step 4: Apply Configuration

For each selected AI tool, add agent-eyes to its MCP config:

**Claude Code** (`~/.claude.json` or `.mcp.json`):
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

If replacing a competitor, remove its entry from the same config.

### Step 5: Migrate References

For files with competitor tool references, replace:

| Competitor tool | agent-eyes replacement |
|----------------|----------------------|
| `mcp__playwright__browser_snapshot` | `mcp__agent-eyes__web_tree` |
| `mcp__playwright__browser_click` | `mcp__agent-eyes__click` |
| `mcp__playwright__browser_type` | `mcp__agent-eyes__type` |
| `mcp__playwright__browser_navigate` | `mcp__agent-eyes__navigate` |
| `mcp__playwright__browser_press_key` | `mcp__agent-eyes__press_key` |
| `mcp__playwright__browser_hover` | `mcp__agent-eyes__hover` |
| `mcp__playwright__browser_evaluate` | `mcp__agent-eyes__js` |
| `mcp__playwright__browser_wait_for` | `mcp__agent-eyes__wait` |
| `mcp__playwright__browser_take_screenshot` | `mcp__agent-eyes__web_tree` |
| `mcp__playwright__browser_file_upload` | `mcp__agent-eyes__upload` |
| `mcp__playwright__browser_handle_dialog` | `mcp__agent-eyes__dialog` |
| `mcp__playwright__browser_tabs` | `mcp__agent-eyes__list_tabs` |
| `mcp__puppeteer__puppeteer_navigate` | `mcp__agent-eyes__navigate` |
| `mcp__puppeteer__puppeteer_screenshot` | `mcp__agent-eyes__web_tree` |
| `mcp__puppeteer__puppeteer_click` | `mcp__agent-eyes__click` |
| `mcp__puppeteer__puppeteer_fill` | `mcp__agent-eyes__fill_form` |
| `mcp__puppeteer__puppeteer_evaluate` | `mcp__agent-eyes__js` |

### Step 6: Verify

Call `mcp__agent-eyes__status` to confirm everything works.

Grep for remaining competitor references — should be zero in the selected scope.

### Step 7: Summary

```
agent-eyes setup complete!

  Configured in: Claude Code, Cursor
  Replaced: Playwright MCP
  Scope: Global
  Files migrated: 3

  Status: ready | macOS | CDP:connected | 3 tabs

Restart your AI tools for changes to take effect.
```
