# Agent Eyes

Agent Eyes is a model-independent computer-use MCP server. It reads native
accessibility trees and sends real keyboard and mouse input so an AI agent can
inspect and operate desktop applications and browsers without being tied to
Claude, Codex, Chrome, or a browser automation framework.

The default is **foreground, native, and browser-agnostic**: inspect all open
tabs, reuse and focus the best existing target, and open a new foreground tab
only when no suitable target exists. Background protocol automation is an
explicit `shadow=true` mode, never an automatic fallback.

## Install to ready

### 1. Install and verify the prerequisites

Agent Eyes supports Python 3.10 through 3.14 on macOS, Windows, and Linux. The
recommended bootstrap uses [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
which can provide a compatible Python automatically on macOS and Windows.

Install `uv` if `uv --version` is not already available:

```bash
# macOS or Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new terminal if the installer updated your `PATH`, then verify it:

```bash
uv --version
```

Linux also needs `/usr/bin/python3` at version 3.10 or newer, `pipx`, and the
distro-provided AT-SPI/PyGObject packages before setup. Agent Eyes deliberately
uses that system Python with `pipx --system-site-packages` so the isolated
persistent runtime can access those system bindings.

```bash
/usr/bin/python3 --version
```

```bash
# Debian or Ubuntu
sudo apt install at-spi2-core python3-gi gir1.2-atspi-2.0 pipx
pipx ensurepath
```

```bash
# Fedora
sudo dnf install at-spi2-core python3-gobject at-spi2-atk pipx
pipx ensurepath
```

For another distribution, install the equivalent AT-SPI2, PyGObject, and
`pipx` packages. Agent Eyes never installs privileged system packages itself.

### 2. Preview setup

Run the newest published setup wizard in an ephemeral `uvx` environment and
inspect every planned change:

```bash
uvx agent-eyes@latest setup --dry-run
```

`@latest` tells `uvx` to refresh the bootstrap package even if an older version
is already installed or cached. Agent Eyes does not install, configure clients,
or update persistent diagnostic state during the preview; `uvx` may still refresh
its own ephemeral package cache.

### 3. Apply setup

```bash
uvx agent-eyes@latest setup
```

Setup is the complete bootstrap. Do not run `agent-eyes install` or `agent-eyes init` afterward;
those are repair and advanced split-flow commands.

Setup displays one combined user-level plan and asks once before applying it.
For an unattended environment where you have already reviewed the dry run:

```bash
uvx agent-eyes@latest setup --yes --non-interactive
```

`--yes` approves only the displayed user-level plan. It does not approve
`sudo`, operating-system permissions, or browser dialogs.

### 4. Verify the installed version and readiness

Setup installs a persistent launcher, so normal use no longer goes through
`uvx`:

```bash
agent-eyes --version
agent-eyes doctor --verbose
```

The target result is `Agent Eyes <version>: ready`. If doctor reports
`degraded`, `setup_required`, or `permission_required`, follow its printed
remediation(s) and rerun the command. Use `agent-eyes doctor --json` for
machine-readable output.

### 5. Restart changed MCP clients

Restart or reload every client that setup says it changed. The restart makes
the client reconnect through the persistent launcher and reload the installed
Agent Eyes skill. A terminal-only version or doctor check does not reload an
already-running MCP client.

### 6. Make the first MCP calls

Ask the restarted MCP client to call `status` once. For a known task, ask it to
use one bounded `execute` call; the target query inventories and reuses open
browser tabs internally:

```text
status {}
execute {"target":{"query":"youtube rollerblade","on_missing":"open","url":"https://www.youtube.com/"},"steps":[{"op":"expect","role":"document","name":"YouTube","match":"contains"}]}
```

`status` should report `ready`. `execute` should stay foreground-native, inspect
open targets first, and avoid asking for a Chrome remote-debugging restart. For
exploration, use one `observe_target` call and then one `execute` call. MCP tools
are called by the AI client, not entered directly in a shell.

## What setup changes

`uvx agent-eyes@latest setup` performs the complete first-run bootstrap:

1. Runs the latest wizard ephemerally and checks the operating system, current
   launcher, native accessibility provider, input provider, and permissions.
2. Reuses a healthy current launcher or installs the exact current Agent Eyes
   platform package persistently: prefers `uv tool` and falls back to `pipx` on
   macOS/Windows, and uses `pipx` with system site packages on Linux.
3. Detects supported MCP clients and preflights their native JSON, JSONC, or TOML
   configuration before writing anything.
4. Writes an absolute persistent executable with `"args": ["serve"]` while
   preserving unrelated configuration.
5. Installs the canonical `agent-eyes/SKILL.md` for Claude Code and Codex, plus
   `agents/openai.yaml` metadata for Codex.
6. Applies MCP configs and skill artifacts as one rollback-capable transaction,
   backing up existing changed files under `~/.agent-eyes/backups/`.
7. Probes the persistent runtime again and reports the final readiness state.

Repeated setup leaves already-current installation, MCP, and skill artifacts
unchanged. It refreshes diagnostic timestamps and, when the selected profile is
core-ready, initialization timestamps. If an exact-version launcher exists but
its precheck reports `setup_required`, setup automatically prepares a forced
repair plan. Use `--repair` explicitly only to force a same-version reinstall of
a damaged environment.

Setup deliberately has clear boundaries:

- It does not install `uv`, Linux `pipx`, or distro system packages.
- It does not run `sudo`.
- It cannot grant macOS Accessibility or other operating-system permissions.
- It cannot approve operating-system or browser dialogs.
- It does not remove Playwright or other competing MCP servers.
- It does not install packages while an MCP stdio server is starting or handling
  a tool call.

Agent Eyes does not depend on Playwright MCP. Playwright-related names are only
recognized while auditing existing client configuration, skill directories, and
agent references; they are not an Agent Eyes runtime or browser requirement.

## Supported MCP clients

Without a selection flag, setup configures every detected supported client.
Repeat `--client ID` to choose specific clients, or use `--all-detected` as an
explicit spelling of the default.

| Client | `--client` ID | Global MCP config | Additional setup artifact |
|---|---|---|---|
| Claude Code | `claude-code` | JSON (`mcpServers`) | `agent-eyes/SKILL.md` |
| Claude Desktop | `claude-desktop` | JSON (`mcpServers`) | None |
| Codex | `codex` | TOML (`mcp_servers`) | `SKILL.md` and `agents/openai.yaml` |
| Cursor | `cursor` | JSON (`mcpServers`) | None |
| VS Code (Copilot) | `vscode` | JSON (`servers`) | None |
| Cline | `cline` | JSON (`mcpServers`) | None |
| Roo Code | `roo-code` | JSON (`mcpServers`) | None |
| Windsurf | `windsurf` | JSON (`mcpServers`) | None |
| Zed | `zed` | JSON (`context_servers`) | None |
| Continue | `continue` | JSON (`mcpServers`) | None |

For example, configure only Codex and Claude Code:

```bash
uvx agent-eyes@latest setup \
  --client codex \
  --client claude-code
```

Setup writes the equivalent of this entry using the actual absolute path:

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

Codex receives the equivalent TOML entry. Windows uses the absolute
`agent-eyes.exe` path. JSON/JSONC/TOML comments and unrelated entries are
preserved where the format supports them; malformed input is rejected, safe
symlink targets are followed, and existing changed artifacts receive recoverable backups.
Existing entries that invoke `agent-eyes` with no arguments remain compatible
because no arguments are an alias for `agent-eyes serve`.

## Browser modes

### Foreground mode: the default

Normal foreground work uses the operating-system accessibility provider. It can
inspect and operate accessible Safari, Firefox, Chromium-family, and other
browsers without a Chrome remote-debugging restart:

1. Call `list_tabs` once with concise task, site, title, or URL terms.
2. Inspect all open tabs across accessibility-visible browser windows.
3. Reuse and focus the best relevant existing target.
4. Open a new foreground tab only when no suitable target exists.
5. Keep the returned provider-qualified `target_id` for exact follow-up actions.

Each native `target_id` is an opaque live handle, for example
`native:78738:w1:t3:r4a1c9e2d7b10f84a`. Do not parse or reconstruct it. Reuse the
exact provider-qualified handle only while it remains in the current short lease;
refresh inventory after navigation, closing, replacement, or reordering.

Some browsers expose limited URLs or DOM details for inactive tabs through
accessibility APIs. That limitation does not cause an automatic switch to a
browser-specific remote mode. The planned optional browser-neutral bridge is
described in the [v2 architecture](https://github.com/jellythomas/agent-eyes/blob/main/docs/plans/2026-07-17-agent-eyes-v2-native-first-design.md).

### Shadow mode: explicit background automation

Use `shadow=true` only when the user explicitly requests background, no-focus,
protocol, or DOM-level browser automation:

```text
list_tabs {"query": "dashboard", "shadow": true}
web_tree {"target_id": "<returned-shadow-target>", "shadow": true}
```

Pass the exact shadow `target_id` and the snapshot returned by `web_tree` to
later actions. `web_tree`, `js`, `dialog`, `upload`, and `pierce` are
protocol-only and reject foreground calls. If the optional provider is not
connected, Agent Eyes reports the capability gap instead of asking to restart
Chrome or silently rerouting normal work.

## Efficient, event-driven operation

Foreground waits subscribe to macOS AXObserver, Windows UI Automation, or Linux
AT-SPI notifications before evaluating their condition. Navigation and explicit
shadow waits use protocol lifecycle/accessibility events. Actions continue as
soon as the expected state becomes observable; bounded adaptive checks are used
only when an event provider cannot be registered. Agent Eyes does not use a
fixed sleep as its page-load strategy. Short key-down/up and drag delays remain
input semantics rather than load polling.

For low latency and model-token use:

- For a known task, call `execute` once with the target and at most eight bounded
  locate/action/expect steps. A target `query` inventories and reuses open tabs
  inside that call; `on_missing: "open"` requires an explicit safe URL.
- For an exploratory task, call `observe_target` once with all needed selectors,
  then call `execute` once with the returned snapshot and target selector: use
  `target.id` for a `native:` browser target or `target.pid` for a PID target. A
  native snapshot binds that exact scope; `execute` still performs one live scoped
  accessibility refresh before locating or acting.
- Call `context` once for orientation and `list_tabs` once for browser ranking.
  Use these primitives only when the transaction fast path does not fit the task.
- Prefer compact accessibility trees over screenshots.
- Preserve observation `snapshot` tokens and use IDs only with their originating
  snapshot.
- Narrow follow-up reads with `find` or `subtree` rather than requesting the full
  tree repeatedly.
- Use condition-specific `wait` calls and verify only the changed state.

## Common CLI syntax

| Command | Purpose |
|---|---|
| `agent-eyes --version` | Print the installed version |
| `agent-eyes setup [options]` | Install/repair, initialize clients, and verify readiness |
| `agent-eyes doctor [--verbose] [--json]` | Run live readiness checks and refresh diagnostic state |
| `agent-eyes install [options]` | Install or repair only the persistent runtime |
| `agent-eyes init [options]` | Configure only MCP clients and supported skills |
| `agent-eyes serve [--log-level LEVEL]` | Run the stdio MCP server |

Common setup/init options include repeatable `--client ID`, `--all-detected`,
`--dry-run`, `--yes`, `--non-interactive`, and `--json`. Setup/install also
accept `--repair`. Use `--help` on any command for its local syntax.

See the [complete CLI reference](https://github.com/jellythomas/agent-eyes/blob/main/docs/api/agent-eyes-cli.md) for every option,
default, profile, JSON field, exit code, environment override, and example.

## MCP tools

The server exposes 30 tools on macOS and 27 on Windows/Linux after omitting the
macOS-only `app`, `window`, and Apple Events compatibility `shadow` tools:

- Orientation: `status`, `context`, `list_apps`, `focused`
- Transaction fast path: `observe_target`, `execute`
- Structured UI: `tree`, `subtree`, `find`, `pierce`
- Input: `click`, `type`, `press_key`, `hover`, `scroll`, `drag`, `fill_form`, `upload`
- Applications/windows on macOS: `app`, `window`
- Browser/web: `list_tabs`, `web_tree`, `navigate`, `js`, `new_tab`, `close_tab`, `dialog`, `wait`
- Explicit background compatibility on macOS: `shadow`
- Readiness compatibility: `install_check` (same live result as `status`)

Preserve each immutable observation snapshot and pass it whenever the action
accepts one. Shadow element actions require the matching snapshot; legacy
foreground `click` and `type` calls can still resolve an unqualified current ID,
but snapshot-qualified calls are safer. See the
[complete MCP tool reference](https://github.com/jellythomas/agent-eyes/blob/main/docs/api/mcp-tools.md) for transport, routing,
parameters, bounds, results, errors, platform availability, safety rules, and
copyable JSON arguments for every tool.

## Readiness states

Agent Eyes derives readiness from live providers rather than an installed flag:

| State | Meaning | Next action |
|---|---|---|
| `ready` | Every capability required by the selected profile is available | None |
| `degraded` | Core foreground use works; an optional/persistent component is missing | Run latest setup when convenient |
| `setup_required` | A component required by the selected profile is missing/failed | Run `uvx agent-eyes@latest setup` |
| `permission_required` | Providers exist but an OS permission/service blocks them | Follow doctor, restart the responsible app, and rerun doctor |

The cached diagnostic manifest is `~/.agent-eyes/readiness.json`. Live probes are
authoritative. Its version, platform, architecture, Python, profile, and
executable fingerprint lets consumers detect whether saved state matches their
current environment.

If the MCP is attached before setup, stdio initialization still completes
without installing or probing permissions. The first `status` or computer-use
call performs a bounded readiness check and returns one structured remediation
when needed; it does not repeatedly prepend setup questions to later results.

## Platform providers

| Platform | Foreground observation | Input | User-controlled requirement |
|---|---|---|---|
| macOS | AXUIElement through PyObjC | Quartz CGEvent | Grant Accessibility to the app that launches Agent Eyes, such as Codex, Claude, Terminal, or iTerm |
| Windows | Microsoft UI Automation through `comtypes` | Win32 SendInput | Normal desktop UI usually has no prompt; elevated/protected UI remains restricted |
| Linux/X11 | AT-SPI2 through PyGObject | XTest through `python-xlib` | Install distro packages and ensure the accessibility service/display session is available |
| Linux/Wayland | AT-SPI2 for structure | Compositor/session dependent | Universal physical input is not implemented; doctor reports the gap |

## Troubleshooting

Start with live diagnostics:

```bash
agent-eyes doctor --verbose
agent-eyes doctor --json
```

Common recovery paths:

- **Missing provider or dependency:** `uvx agent-eyes@latest setup`.
- **Damaged same-version runtime:** `agent-eyes setup --repair`.
- **Permission denied:** grant permission to the app that launches Agent Eyes,
  restart that app, and rerun doctor.
- **Malformed client config:** repair the reported JSON, JSONC, or TOML file, then
  rerun `agent-eyes init --dry-run`.
- **Old `uvx` MCP entry:** rerun `agent-eyes init`; client configs should use the
  persistent absolute launcher.
- **Interrupted setup:** rerun the same setup command. Installation and config
  writes are lock-protected and idempotent.
- **Client still shows old tools:** restart/reload the changed client.

Backups of existing changed configs and skills are under
`~/.agent-eyes/backups/`.

## Upgrade, repair, and removal

Upgrade to the latest release and reapply the complete verified setup:

```bash
uvx agent-eyes@latest setup
agent-eyes --version
agent-eyes doctor --verbose
```

Use `--repair` only to force a same-version reinstall when the persistent
environment is damaged.

To remove only the persistent executable, use the manager reported by setup:

```bash
uv tool uninstall agent-eyes
# or
pipx uninstall agent-eyes
```

Then remove the `agent-eyes` entry from MCP client configs and, if present, the
installed `agent-eyes` skill directories. Automatic cross-client uninstall and
rollback orchestration is not implemented.

## Reproducible benchmarks

The checked-in pre-hardening baseline and benchmark programs use deterministic
fixtures so results are attributable to Agent Eyes rather than a website or
network. The historical 0.9.0 branch reference and the 0.10.0 release-candidate
reference used macOS arm64 and Python 3.12. Benchmarks use 30 measured samples
after three warmups and p95=`sorted[ceil(0.95*n)-1]` unless a result records a
different protocol.

| Gate | Before | Post-tag branch | Change |
|---|---:|---:|---:|
| Server import p95 | Not comparable* | 283.72 ms | Passes <=450 ms gate |
| MCP initialize + tools/list p95 | Not comparable* | 314.00 ms | Passes <=500 ms gate |
| Immediate event completion p95 | 4.750 ms | 0.579 ms | 87.8% lower |
| Format 1,000 targets p95 | 6.839 ms | 3.298 ms | 51.8% lower |
| Compact tool-catalog bytes | 18,881 | 15,143 | 19.8% lower |
| Provider calls for 32 identical observations | 32 | 1 | 96.9% fewer |
| CDP enrichment, 60 elements / 1 ms RTT | 182 round trips | 61 round trips | 66.5% fewer; 2.92x measured speedup |

*The current startup schema measures server import in a fresh interpreter and
MCP launch through `tools/list`. The earlier baseline included interpreter
lifecycle and transport cleanup, so those startup values are not like-for-like.

The v0.10.0 bounded-transaction fixture records latency without using hosted
wall time as a CI gate. Deterministic call, scan, activation, dispatch, output,
and cleanup counts are the portable release gates:

| v0.10.0 transaction evidence | Median | p95 |
|---|---:|---:|
| Known one-call inline-comment journey | 1.484 ms | 2.006 ms |
| Discovery (`observe_target` then `execute`) | 1.797 ms | 2.672 ms |
| Maximum `execute` output | 174 bytes | limit 2,048 bytes |
| Maximum `observe_target` output | 505 bytes | limit 4,096 bytes |

The fixture performs exactly one external write on success, zero foreground
shadow probes, at most one activation, and at most one full scan before the
first mutation. The transaction stress gate cancels every named boundary,
serializes 32 queued foreground calls, and runs 10,000 resource lifecycles with
zero retained measured resources and RSS growth bounded by max(10 MiB, 5%).

The journey benchmark verifies that an existing YouTube target is activated with
zero new tabs and zero implicit shadow probes, while an absent target opens one
foreground tab. It deliberately excludes live browser, rendering, and network
latency. Reproduce the benchmark classes with:

```bash
uv run python benchmarks/benchmark_startup.py
uv run python benchmarks/benchmark_runtime.py
uv run python benchmarks/benchmark_cdp_bounds.py
uv run python benchmarks/benchmark_journeys.py
uv run python benchmarks/benchmark_transactions.py
uv run python -m benchmarks.stress_transactions
uv run python benchmarks/benchmark_live_transactions.py \
  --executable "$(command -v agent-eyes)" \
  --query "a unique title or URL already open in your browser" \
  --wheel dist/agent_eyes-0.10.0-py3-none-any.whl
uv run python benchmarks/stress_concurrency.py
```

The live benchmark uses one real MCP session, redacts the supplied query from its
JSON result, never requests shadow mode, and restores the previously selected
native target when the provider exposed one. Pass `--wheel dist/<wheel>.whl` when
recording release evidence so the result includes the exact artifact SHA-256.
For a non-mutating real `execute` measurement, also pass a uniquely matching
`--selector-role` and optional `--selector-name`; the benchmark executes only a
read-only `expect` step. Reference-agent JSON/JSONL traces can be summarized
without content fields using `benchmarks/analyze_reference_agent_trace.py`.

The stress suite also validates bounded observation-store memory and 100
barrier-synchronized cross-process setup-lock acquisitions. Machine-readable
results and the baseline protocol are under `benchmarks/results/` and
`benchmarks/baselines/`.

## Development

```bash
uv sync --all-groups
uv run python -m pytest -q
uvx --from ruff==0.15.7 ruff check src benchmarks scripts
uvx bandit -r src scripts -q -ll
uv run agent-eyes doctor --verbose
```

The project uses MCP over stdio, so runtime logs must go to stderr; stdout is
reserved for JSON-RPC frames. Release jobs build from two independent clean Git
archives, require reproducible wheel/sdist output, rebuild the wheel from the
normalized sdist, and smoke-test the exact artifact selected for upload.

## License

MIT
