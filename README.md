# Agent Eyes

Agent Eyes is a model-independent computer-use MCP server. It reads native accessibility trees and sends real keyboard/mouse input so an AI agent can inspect and operate desktop applications without depending on Claude, Codex, or a particular browser automation framework.

The default policy is foreground and browser-agnostic: inspect the live desktop and all accessibility-visible browser windows/tabs, reuse and focus the best existing target, and open a new tab only when no suitable target exists. Shadow/background automation is an explicit mode, not the normal fallback.

## First-time setup

Run one guided bootstrap command:

```bash
uvx agent-eyes setup
```

If you attach the MCP before running setup, the stdio handshake still completes
without probing permissions or installing anything. The first `status` or
computer-use call performs a bounded live readiness check. When a required
provider or permission is missing, action tools return one structured
`setup_required` or `permission_required` error with the recovery command. Run
that command in a terminal; the MCP server never installs packages from inside a
tool call.

`uvx` is used only to bootstrap setup. The wizard:

1. Checks the current version, persistent launcher, operating system, native accessibility provider, input provider, and permissions.
2. Shows the exact persistent-install command and every MCP config/skill artifact it plans to change.
3. Asks once before applying user-level changes.
4. Reuses a healthy current launcher without reinstalling; otherwise installs the current Agent Eyes version into an isolated `uv tool` or `pipx` environment.
5. Atomically configures detected MCP clients with the stable launcher path and synchronizes the canonical Agent Eyes skill, with backups for existing changed artifacts.
6. Verifies the installed providers and reports any permission step still requiring you.

Setup never silently runs `sudo`, grants OS permissions, removes other MCP servers, or installs packages while the MCP stdio server is starting.

Use `--repair` only to force a reinstall after an upgrade or damaged installation. Normal repeated setup is a fast live check plus idempotent configuration.

Preview without changing anything:

```bash
uvx agent-eyes setup --dry-run
```

For automated user-level setup:

```bash
uvx agent-eyes setup --yes --non-interactive
```

`--yes` does not approve sudo, operating-system permissions, or browser dialogs.

## MCP configuration

Setup writes an absolute persistent launcher. A resulting configuration looks like:

```json
{
  "mcpServers": {
    "agent-eyes": {
      "command": "/Users/you/.local/bin/agent-eyes",
      "args": ["serve"]
    }
  }
}
```

Codex receives the equivalent entry in `~/.codex/config.toml`; JSON and JSONC clients keep their native format and unrelated entries. Claude Code and Codex also receive the canonical `agent-eyes/SKILL.md`, and Codex receives its supported `agents/openai.yaml` metadata. MCP configs and skill files are preflighted and applied as one rollback-capable transaction. Windows uses the equivalent absolute `agent-eyes.exe` path. Restart a changed MCP client after setup so it reconnects through the persistent launcher and reloads the skill.

Existing configurations that invoke `agent-eyes` with no arguments remain compatible; no arguments are an alias for `agent-eyes serve`.

## Commands

```text
agent-eyes setup [--yes] [--non-interactive] [--client CLIENT | --all-detected] [--dry-run] [--repair] [--json]
agent-eyes doctor [--json] [--verbose] [--profile PROFILE] [--refresh]
agent-eyes install [--yes] [--dry-run] [--repair] [--json] [--profile PROFILE]
agent-eyes init [--yes] [--non-interactive] [--client CLIENT] [--all-detected] [--dry-run] [--json]
agent-eyes serve
```

- `agent-eyes setup` runs the complete guided install, client initialization, and verification flow.
- `agent-eyes doctor` performs live checks and refreshes `~/.agent-eyes/readiness.json`; it does not install packages or change MCP client configs.
- `agent-eyes install` performs only the persistent, current-platform installation phase.
- `agent-eyes init` preflights every selected MCP config and supported skill artifact before writing, supports JSON/JSONC/TOML, preserves unrelated tools and comments, follows safe symlink targets, and applies the group atomically with backups.
- `agent-eyes serve` starts the stdio MCP server. It performs no package-manager or network work.

You can rerun `agent-eyes setup --repair` safely after an upgrade or failed setup. The configuration operations are idempotent.

## Readiness states

Agent Eyes derives readiness from live providers rather than trusting an “installed” Boolean:

| State | Meaning | Action |
|---|---|---|
| `ready` | Required foreground observation and input capabilities work | None |
| `degraded` | Core foreground use works, but an optional or persistent component is missing | Run `agent-eyes setup` when convenient |
| `setup_required` | A required native/input provider is missing or failed | Run `agent-eyes setup` |
| `permission_required` | Providers are installed but an OS permission/service needs user action | Follow `agent-eyes doctor --verbose`, then rerun it |

When required capabilities are unavailable, the MCP still completes its handshake and exposes a compact `status` tool with the recovery command. It does not repeatedly prepend setup questions to normal tool results.

The cached manifest is `~/.agent-eyes/readiness.json`. Live checks remain authoritative; version, platform, architecture, Python, profile, or executable changes invalidate the cached identity.

## Platform guidance

| Platform | Foreground provider | Input provider | User-controlled setup |
|---|---|---|---|
| macOS | AXUIElement via PyObjC | Quartz CGEvent | Grant Accessibility to the app responsible for launching Agent Eyes (for example Codex, Claude, Terminal, or iTerm) |
| Windows | Microsoft UI Automation via `comtypes` | Win32 SendInput | Standard desktop UI usually needs no prompt; elevated/protected UI has additional Windows restrictions |
| Linux/X11 | AT-SPI2 via PyGObject | XTest via `python-xlib` | Install distro AT-SPI/PyGObject packages and ensure the accessibility service/display session is available |
| Linux/Wayland | AT-SPI2 for structure | Depends on compositor/session | Universal physical input is not implemented yet; doctor reports the capability gap instead of crashing |

Linux distributions provide GI/AT-SPI bindings as system packages. Install them before rerunning setup:

```bash
# Debian/Ubuntu (pipx is also required for the persistent Linux tool)
sudo apt install at-spi2-core python3-gi gir1.2-atspi-2.0
python3 -m pip install --user pipx && python3 -m pipx ensurepath

# Fedora (pipx is also required for the persistent Linux tool)
sudo dnf install at-spi2-core python3-gobject at-spi2-atk pipx
```

Agent Eyes does not modify system Python or use `--break-system-packages`.

## Browser behavior

Normal foreground browser work does not require a Chrome remote-debugging restart.

The operating-system accessibility provider can inspect and operate Safari, Firefox, Chromium-family browsers, and other accessible browsers as ordinary applications. The target policy is:

1. Refresh every live accessibility-visible browser/window/tab target.
2. Score all open tabs against the requested site, title, URL, and task.
3. Focus and reuse the best relevant existing tab.
4. Open a new foreground tab only if there is no suitable match.
5. Use a background/shadow provider only when the caller explicitly requests it.

`list_tabs`, `navigate`, `new_tab`, and `close_tab` use the native foreground policy by default. Returned targets have provider-qualified IDs such as `native:78738:w1:t3:h4a1c9e2d7b10`; the content fingerprint makes destructive IDs stale when a tab is replaced or reordered, so prefer those IDs over a mutable list position. `web_tree`, `js`, `dialog`, `upload`, and `pierce` are protocol-only tools and reject the call unless `shadow=true` was explicitly supplied. Agent Eyes never asks for a Chrome remote-debugging restart during normal foreground work.

Current limitation: native accessibility exposes the tab labels and controls that each browser makes available to assistive technology, but some browsers do not expose every inactive tab's exact URL or page DOM. The planned shared WebExtension bridge adds exact tab/URL/navigation metadata while keeping one browser-neutral protocol. Chromium and Firefox can share most extension code but require different native-host manifests; Safari uses the same WebExtension core inside its Apple packaging/native-app bridge. These packaging adapters do not leak into the MCP tool contract. See the [v2 architecture](docs/plans/2026-07-17-agent-eyes-v2-native-first-design.md).

The bridge is therefore not the universal browser-control dependency. Native
accessibility remains the always-on foreground plane. A future optional bridge
can share its WebExtension logic and Agent Eyes protocol, while retaining small
browser packaging/permission adapters: Chromium and Firefox native-messaging
manifests differ, and Safari Web Extensions ship as Apple app extensions.
[WebDriver BiDi](https://www.w3.org/TR/webdriver-bidi/) provides a promising
evented cross-browser protocol, but it creates a WebDriver session and remains a
Working Draft; Agent Eyes treats it as a future explicit shadow provider rather
than a way to seize every already-open authenticated browser session. The Linux
broker direction similarly targets the
[XDG RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
and [libei](https://libinput.pages.freedesktop.org/libei/) without changing the
MCP contract.

## Completion and performance model

Foreground waits subscribe to macOS AXObserver, Windows UI Automation, or Linux AT-SPI notifications before evaluating the condition. Navigation and explicit shadow waits use protocol lifecycle/accessibility events. Actions return as soon as the expected state is observable; bounded adaptive checks are used only when an OS notification provider cannot be registered. Delays that separate physical key-down/up or drag events remain input semantics, not page-load polling.

To keep model context small, orientation is compact by default, trees have bounded output budgets, element text is normalized/truncated, and browser target IDs remain stable between ranking changes. Ask for a targeted `find`, `subtree`, or `wait` result instead of repeatedly requesting a full tree.

## MCP tools

Agent Eyes currently exposes 28 tools on macOS and 26 on Windows/Linux when the
foreground core is ready. `app` and `window` are omitted outside macOS until
their exact-target mutation contracts are implemented on those platforms:

- Orientation: `status`, `context`, `list_apps`, `focused`
- Structured UI: `tree`, `subtree`, `find`, `pierce`
- Input: `click`, `type`, `press_key`, `hover`, `scroll`, `drag`, `fill_form`, `upload`
- Applications/windows: `app`, `window`
- Browser/web: `list_tabs`, `web_tree`, `navigate`, `js`, `new_tab`, `close_tab`, `dialog`, `wait`
- Explicit background mode: `shadow`
- Compatibility: `install_check` (same live result as `status`)

Element IDs from a tree can be passed to action tools. Structured accessibility output is preferred over screenshots to reduce latency and token use.

## Reproducible benchmarks

The checked-in pre-hardening baseline and benchmark programs use deterministic
fixtures so results are attributable to Agent Eyes rather than a website or
network. The reference run below used macOS arm64, Python 3.12, 30 measured
samples after 3 warmups, and p95=`sorted[ceil(0.95*n)-1]`.

| Gate | Before | 0.9.0 | Change |
|---|---:|---:|---:|
| Server import p95 | 592.70 ms | 457.79 ms | 22.8% lower |
| MCP initialize + tools/list p95 | 584.30 ms | 437.04 ms | 25.2% lower |
| Immediate event completion p95 | 4.750 ms | 0.572 ms | 88.0% lower |
| Format 1,000 targets p95 | 6.839 ms | 3.369 ms | 50.7% lower |
| Compact tool-catalog bytes | 18,881 | 12,544 | 33.6% lower |
| Provider calls for 32 identical observations | 32 | 1 | 96.9% fewer |
| CDP enrichment, 60 elements / 1 ms RTT | 182 round trips | 61 round trips | 66.5% fewer; 2.92x measured speedup |

The journey benchmark asserts that an existing YouTube target is activated with
zero new tabs and zero implicit shadow probes, and that an absent target opens
exactly one foreground tab. It does not include real provider, browser-rendering,
or network latency. Reproduce every reported class of measurement with:

```bash
uv run python benchmarks/benchmark_startup.py
uv run python benchmarks/benchmark_runtime.py
uv run python benchmarks/benchmark_cdp_bounds.py
uv run python benchmarks/benchmark_journeys.py
uv run python benchmarks/stress_concurrency.py
```

Machine-readable results and the baseline protocol are under
`benchmarks/results/` and `benchmarks/baselines/`.

## Troubleshooting

Start with:

```bash
agent-eyes doctor --verbose
agent-eyes doctor --json
```

Common recovery steps:

- Missing provider: rerun `agent-eyes setup --repair`.
- Permission denied: grant the permission to the responsible launcher, restart that app, and rerun doctor.
- Config error: Agent Eyes refuses to overwrite malformed JSON, JSONC, or TOML. Repair the reported file and rerun `agent-eyes init`.
- Old `uvx` MCP entry: rerun init so the client uses the persistent absolute launcher.
- Interrupted setup: rerun the same setup command; verified steps and idempotent config entries are safe to repeat.

Backups of existing changed MCP configs and skill artifacts are stored under `~/.agent-eyes/backups/`.

## Upgrade and removal

Upgrade the persistent tool, then reverify:

```bash
uv tool upgrade agent-eyes
agent-eyes setup --repair
```

For pipx installations, use `pipx upgrade agent-eyes`. To remove the executable, use `uv tool uninstall agent-eyes` or `pipx uninstall agent-eyes`, then remove the `agent-eyes` entry from MCP client configs. Automatic rollback/uninstall orchestration is not implemented yet.

## Development

```bash
uv sync --all-groups
uv run pytest -q
uv run pytest --cov=agent_eyes.coordinator --cov=agent_eyes.input_validation --cov=agent_eyes.observations --cov=agent_eyes.operation --cov=agent_eyes.tool_contract --cov-report=term-missing tests/test_coordinator_concurrency.py tests/test_input_validation.py tests/test_observation_store.py tests/test_operation_budget.py tests/test_tool_contract.py
uvx ruff check src benchmarks
uvx bandit -r src -q -ll
uv run agent-eyes doctor --verbose
```

Python 3.10 through 3.14 is tested. The project uses MCP over stdio, so runtime logging must go to stderr; stdout is reserved for JSON-RPC frames.

## License

MIT
