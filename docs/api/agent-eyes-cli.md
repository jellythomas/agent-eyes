# Agent Eyes CLI reference

**Documented release:** 0.10.0

The `agent-eyes` command installs, configures, verifies, and serves the Agent
Eyes MCP server. Python 3.10 through 3.14 is supported.

For a first-time walkthrough, start with the [project README](../../README.md).
This page is the exhaustive command reference.

## Installation

The recommended bootstrap runs the latest published setup wizard in an
ephemeral uv tool environment:

```bash
uvx agent-eyes@latest setup
```

Setup installs the same Agent Eyes version persistently with the current
platform provider, configures selected MCP clients, synchronizes supported
skills, and verifies readiness. Subsequent commands use the persistent
`agent-eyes` launcher.

Setup is the complete first-run flow. Do not run `agent-eyes install` or `agent-eyes init` afterward;
they are repair and advanced split-flow commands.

Preview the complete Agent Eyes plan without installing, configuring clients, or
updating persistent Agent Eyes state:

```bash
uvx agent-eyes@latest setup --dry-run
```

The `uvx` bootstrap itself may refresh its ephemeral package cache.

## Usage

```text
agent-eyes [-h] [--version] {serve,doctor,install,init,setup} ...
```

Running `agent-eyes` without a subcommand is an alias for `agent-eyes serve`.

### Top-level options

| Option | Description |
|---|---|
| `-h`, `--help` | Show top-level help and exit. |
| `--version` | Print `agent-eyes <version>` and exit. |

Examples:

```bash
agent-eyes --version
agent-eyes --help
```

## Exit codes

| Code | Name | Meaning |
|---:|---|---|
| `0` | `OK` | Success, ready, already current, or a successful dry run. |
| `1` | `ERROR` | Installation, configuration, or unexpected general failure. |
| `2` | `USAGE` | Invalid arguments, option combinations, or client identifiers. |
| `3` | `SETUP_REQUIRED` | A required provider, dependency, or persistent setup component is unavailable. |
| `4` | `ACTION_REQUIRED` | OS permission/user action is required, or readiness is degraded. |
| `5` | `CANCELLED` | The user declined a mutating plan, or non-interactive mutation lacked `--yes`. Dry runs return `0`. |

## Shared options

### Capability profile

`doctor` and `setup` accept:

| Option | Type | Default | Description |
|---|---|---|---|
| `--profile` | `standard` or `full` | `standard` | Select the readiness profile. `standard` requires foreground observation/input; `full` also requires a stable, non-disposable runtime or an explicit persistent launcher. |

`install` continues to accept `--profile` for CLI compatibility. Installation
always selects the package extra required by the current operating system, so
the value does not change the package installed.

### Machine-readable output

`doctor`, `install`, `init`, and `setup` accept `--json`. JSON is written to
stdout with stable field names. Human diagnostics and MCP runtime logs use
stderr where applicable.

Command failures emitted through the shared CLI error path use:

```json
{
  "error": "A concise recovery-safe message",
  "status": "error"
}
```

### Mutation controls

`install`, `init`, and `setup` accept:

| Option | Default | Description |
|---|---:|---|
| `--yes` | `false` | Approve the computed user-level plan. It does not grant OS permissions, approve dialogs, or authorize `sudo`. |
| `--non-interactive` | `false` | Never prompt. Applying changes also requires `--yes`; otherwise the command exits `5`. |
| `--dry-run` | `false` | Preflight and display the plan without applying persistent Agent Eyes changes. |

Input that is not attached to a TTY is also treated as non-interactive: applying
without `--yes` exits `5`, even when `--non-interactive` was omitted. With
`--json --yes`, the command emits only the final JSON object rather than a human
plan before applying it. Automation should review a `--dry-run --json` result
first, then approve the same intended selection in a separate invocation.

### Client selection

`init` and `setup` accept one of:

| Option | Default | Description |
|---|---:|---|
| `--client <id>` | none | Configure one supported client. Repeat the option to select multiple clients. |
| `--all-detected` | implicit | Explicitly configure every detected client. Omitting both selection options has the same selection behavior. |

`--client` and `--all-detected` are mutually exclusive. Explicit `--client`
selection can configure a known client even when its normal detection directory
does not yet exist.

## `agent-eyes serve`

Start the MCP server over stdio.

```text
agent-eyes serve [-h] [--log-level {debug,info,warning,error}]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | — | Show command help and exit. |
| `--log-level` | `info` | Set Python runtime logging to `debug`, `info`, `warning`, or `error`. |

Side effects:

- Opens an MCP stdio session.
- Reserves stdout for JSON-RPC frames.
- Does not install packages, edit client configuration, or open a browser during startup.

Example MCP entry:

```json
{
  "command": "/absolute/path/to/agent-eyes",
  "args": ["serve"]
}
```

Do not normally run `serve` in an interactive terminal; an MCP client launches
it using the persistent absolute path written by `setup` or `init`.

## `agent-eyes doctor`

Run live provider and permission checks, then refresh diagnostic state.

```text
agent-eyes doctor [-h] [--profile {standard,full}] [--json] [--verbose]
                  [--refresh]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | — | Show command help and exit. |
| `--profile` | `standard` | Readiness profile to evaluate. |
| `--json` | `false` | Emit the readiness report as JSON. |
| `--verbose` | `false` | Show every capability check in text output. |
| `--refresh` | `false` | Compatibility flag. `doctor` already performs a live probe on every invocation. |

Doctor saves the resulting diagnostic manifest to
`$AGENT_EYES_STATE_DIR/readiness.json`, or
`~/.agent-eyes/readiness.json` by default. The cache is diagnostic state; live
provider checks remain authoritative.

JSON fields:

| Field | Meaning |
|---|---|
| `schema_version` | Readiness manifest schema version. |
| `agent_eyes_version` | Version that produced the report. |
| `checked_at` | UTC check timestamp. |
| `fingerprint` | Version, platform, architecture, Python, profile, and executable identity. |
| `status` | `ready`, `degraded`, `setup_required`, or `permission_required`. |
| `core_ready` | Whether every capability required by the selected profile is available. |
| `recovery_command` | Recommended CLI recovery command. |
| `capabilities` | Per-capability status, detail, requirement, version, and remediation. |

`fingerprint` contains these string fields: `agent_eyes_version`, `platform`,
`architecture`, `python`, `executable`, and `profile`. Each `capabilities` item
contains:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Capability identifier. |
| `required` | boolean | Whether the selected profile requires it. |
| `status` | string | `available`, `missing`, `permission_required`, or `error`. |
| `detail` | string | Safe diagnostic detail. |
| `remediation` | string or null | User action when unavailable. |
| `version` | string or null | Detected provider/component version when available. |

Examples:

```bash
agent-eyes doctor --verbose
agent-eyes doctor --profile full --json
```

## `agent-eyes install`

Install or repair only the persistent Agent Eyes tool environment. This command
does not configure MCP clients.

```text
agent-eyes install [-h] [--profile {standard,full}] [--json] [--yes]
                   [--non-interactive] [--dry-run] [--repair]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | — | Show command help and exit. |
| `--profile` | `standard` | Accepted for compatibility; it does not alter the platform package selected. |
| `--json` | `false` | Emit the plan/result as JSON. |
| `--yes` | `false` | Approve the computed installation plan. |
| `--non-interactive` | `false` | Disable prompts; applying requires `--yes`. |
| `--dry-run` | `false` | When install/repair is needed, display the exact package-manager command without applying it; an already-current runtime has no command to run. |
| `--repair` | `false` | Force reinstall of the exact current version. |

Manager selection:

- macOS and Windows prefer `uv`, then `pipx`.
- Linux requires `pipx` so the isolated environment can use distro-provided
  AT-SPI/PyGObject through `--system-site-packages`. It always selects
  `/usr/bin/python3`, which must be Python 3.10 or newer.
- The installed requirement is exact-version and platform-qualified, such as
  `agent-eyes[macos]==0.10.0`.

JSON status is `already_current`, `planned`, `installed`, `cancelled`, or
`error`. The complete successful/planned response shape is:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | Lifecycle status listed above. |
| `executable` | string | Resolved persistent launcher path. |
| `install` | object or null | Null when already current; otherwise contains `plan` and `result`. |
| `dry_run` | boolean | Whether installation was only planned. |
| `error` | string, conditional | Present for an apply failure that retains the install payload. Preflight/shared failures use the smaller shared error object documented above. |

`install.plan` contains `manager` (string), `command` (string array),
`description` (string), `privileged` (boolean), and `version` (string).
`install.result` is null before application; afterward it contains `applied`,
`cancelled`, `dry_run`, and `already_current` booleans plus nullable string
`error`.

Examples:

```bash
agent-eyes install --dry-run
agent-eyes install
agent-eyes install --repair --yes --non-interactive --json
```

## `agent-eyes init`

Configure selected MCP clients using an existing current persistent launcher.

```text
agent-eyes init [-h] [--json] [--yes] [--non-interactive] [--dry-run]
                [--client CLIENT | --all-detected]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | — | Show command help and exit. |
| `--json` | `false` | Emit selected clients and artifact changes as JSON. |
| `--yes` | `false` | Approve the computed configuration transaction. |
| `--non-interactive` | `false` | Disable prompts; applying requires `--yes`. |
| `--dry-run` | `false` | Preflight every artifact and write nothing. |
| `--client <id>` | detected clients | Select one client; repeatable. |
| `--all-detected` | implicit | Explicitly select every detected client. |

Init requires the persistent launcher to report the exact current Agent Eyes
version. It preflights every selected MCP configuration and skill artifact
before writing, then applies the group as one rollback-capable transaction.
Unrelated MCP servers and configuration entries are preserved.

JSON status is `planned`, `configured`, `cancelled`, or `error`. Its fields are:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | Lifecycle status listed above. |
| `executable` | string | Exact-version persistent launcher path. |
| `clients` | object array | Artifact plan records, not a deduplicated list of client IDs. |
| `changes` | object array | Preflight or applied result for each artifact. |
| `dry_run` | boolean | Whether writes were skipped. |

Each `clients` artifact-plan record always has `artifact`, `client`, and `path`.
MCP records also have `servers_key`, `is_zed`, and `format`; skill and
skill-metadata records do not. `artifact` is `mcp`, `skill`, or
`skill-metadata`. Every `changes` record contains `artifact`, `client`, `path`,
`changed`, `applied`, and nullable `backup`.

Examples:

```bash
agent-eyes init --dry-run
agent-eyes init --client codex
agent-eyes init --client codex --client claude-code
agent-eyes init --all-detected --yes --non-interactive --json
```

Restart every client whose MCP or skill artifact changed.

## `agent-eyes setup`

Run persistent installation, client initialization, and final readiness
verification as one guided flow.

```text
agent-eyes setup [-h] [--profile {standard,full}] [--json] [--yes]
                 [--non-interactive] [--dry-run]
                 [--client CLIENT | --all-detected] [--repair]
```

| Option | Default | Description |
|---|---|---|
| `-h`, `--help` | — | Show command help and exit. |
| `--profile` | `standard` | Final readiness profile to verify. |
| `--json` | `false` | Emit plan, precheck, changes, and final readiness as JSON. |
| `--yes` | `false` | Approve the computed user-level setup plan. |
| `--non-interactive` | `false` | Disable prompts; applying requires `--yes`. |
| `--dry-run` | `false` | Preflight install/config artifacts without changing persistent Agent Eyes state. Temporary diagnostic state may be created and deleted during precheck. |
| `--client <id>` | detected clients | Select one client; repeatable. |
| `--all-detected` | implicit | Explicitly select every detected client. |
| `--repair` | `false` | Reinstall the persistent package and reapply/reverify configuration. |

Setup performs these phases:

1. Resolve a healthy exact-version persistent launcher or prepare an install plan.
2. Detect/select MCP clients.
3. Preflight every configuration and skill artifact.
4. Display one complete user-level plan.
5. Apply only after approval.
6. Verify the installed launcher and live platform providers.
7. Save readiness state. Save initialization metadata only when `core_ready` is
   true for the selected profile.

If an exact-version launcher passes version validation but its precheck reports
`setup_required`, setup automatically replaces the keep-current decision with a
forced same-version repair plan.

JSON status is `planned`, `cancelled`, one of the four readiness states
(`ready`, `degraded`, `setup_required`, `permission_required`), or `error`. Its
fields are:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | Plan/cancellation status or final readiness state. |
| `install` | object or null | Same `plan`/`result` object documented for `install`; null when the launcher is kept. |
| `executable` | string | Persistent launcher path selected by the plan. |
| `clients` | object array | Artifact-plan records with the same shape as `init.clients`. |
| `changes` | object array | Records with the same shape as `init.changes`. |
| `precheck` | object | Full readiness-report shape documented under `doctor`. |
| `profile` | string | `standard` or `full`. |
| `dry_run` | boolean | Whether setup stopped after planning. |
| `readiness` | object, applied only | Final full readiness report after installation/configuration. |

Shared preflight or apply failures can instead use only `status: "error"` and
`error`, as documented under machine-readable output.

Examples:

```bash
uvx agent-eyes@latest setup --dry-run
uvx agent-eyes@latest setup
uvx agent-eyes@latest setup --client codex
uvx agent-eyes@latest setup --all-detected --yes --non-interactive --json
agent-eyes setup --repair
```

## Supported MCP clients

| Client ID | Client | MCP format/key | Synchronized artifacts |
|---|---|---|---|
| `claude-code` | Claude Code | JSON / `mcpServers` | MCP entry and `agent-eyes/SKILL.md` |
| `claude-desktop` | Claude Desktop | JSON / `mcpServers` | MCP entry |
| `codex` | Codex | TOML / `mcp_servers` | MCP entry, `agent-eyes/SKILL.md`, and `agents/openai.yaml` |
| `cursor` | Cursor | JSON / `mcpServers` | MCP entry |
| `vscode` | VS Code (Copilot) | JSON / `servers` | MCP entry |
| `cline` | Cline | JSON / `mcpServers` | MCP entry |
| `roo-code` | Roo Code | JSON / `mcpServers` | MCP entry |
| `windsurf` | Windsurf | JSON / `mcpServers` | MCP entry |
| `zed` | Zed | JSON / `context_servers` | Custom MCP entry |
| `continue` | Continue | JSON / `mcpServers` | MCP entry |

Common user-level paths:

| Client | Path |
|---|---|
| Claude Code | `~/.claude.json` |
| Codex | `~/.codex/config.toml` |
| Cursor | `~/.cursor/mcp.json` |
| Continue | `~/.continue/config.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Zed | `$XDG_CONFIG_HOME/zed/settings.json` or platform equivalent |
| Claude Desktop | Platform application-config directory / `claude_desktop_config.json` |
| VS Code/Cline/Roo Code | Platform VS Code user/global-storage directory |

## Environment variables

| Variable | Purpose |
|---|---|
| `AGENT_EYES_STATE_DIR` | Override the default `~/.agent-eyes` state, readiness, lock, and backup root. Primarily useful for isolated automation/tests. |
| `PIPX_BIN_DIR` | Override the directory used to locate the persistent pipx launcher. |
| `XDG_CONFIG_HOME` | Linux configuration root used while detecting supported clients. |
| `APPDATA` | Windows roaming configuration root used while detecting supported clients. |

## Upgrade

Run the newest published wizard so it can install the new exact-version platform
package with the appropriate manager and resynchronize every selected client:

```bash
uvx agent-eyes@latest setup
agent-eyes --version
agent-eyes doctor --verbose
```

Use `agent-eyes setup --repair` only to force a same-version reinstall when the
persistent environment is damaged. Restart changed MCP clients after setup.

## Removal

Remove the persistent executable with the manager that owns it:

```bash
uv tool uninstall agent-eyes
```

or:

```bash
pipx uninstall agent-eyes
```

Then remove only the `agent-eyes` MCP entry from configured clients. Optionally
remove synchronized `agent-eyes` skill directories and
`~/.agent-eyes/readiness.json` after preserving any backups you still need.

Automatic uninstall/rollback orchestration is not currently implemented.
