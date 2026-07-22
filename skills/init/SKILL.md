---
name: init
description: >
  Safely configure Agent Eyes in supported MCP clients after runtime
  installation. Use for config previews, selected-client setup, canonical skill
  synchronization, backups, idempotent reconfiguration, and restart guidance.
---

# Initialize MCP clients

Use the CLI instead of hand-editing client configuration.

## Configure detected clients

```bash
agent-eyes --version
agent-eyes doctor --verbose
agent-eyes init --dry-run
agent-eyes init
```

Review the complete dry-run plan before applying it. Restart every client the
result reports as changed.

Configure one or more supported clients when requested:

```bash
agent-eyes init --client codex --client claude-code --dry-run
agent-eyes init --client codex --client claude-code
```

Without `--client`, init selects every detected supported client.
`--all-detected` is the explicit spelling of that default.

The initializer:

- writes an absolute persistent `agent-eyes` executable with `args: ["serve"]`;
- supports the client-native JSON, JSONC, and TOML configuration paths;
- synchronizes the canonical Agent Eyes skill for Claude Code and Codex;
- installs `agents/openai.yaml` metadata for Codex;
- preserves unrelated configuration and competing MCP servers;
- rejects malformed input and unsafe paths rather than replacing them;
- preflights all selected config/skill artifacts and applies one rollback-capable
  transaction with backups for existing changed files;
- leaves already-current MCP/skill artifacts unchanged, refreshes initialization
  metadata, and reports which clients require restart/reload.

`agent-eyes init` does not install or repair the runtime. If the current
persistent launcher is missing, run:

```bash
uvx agent-eyes@latest setup --dry-run
uvx agent-eyes@latest setup
```

Do not install packages or grant permissions from inside an MCP tool call.
