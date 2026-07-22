---
name: install
description: >
  Guide Agent Eyes first-time setup, runtime-only installation, dependency and
  permission diagnosis, repair, or upgrade. Use the model-independent CLI and
  never install packages from inside an MCP server process or tool call.
---

# Install Agent Eyes

Use the CLI as the source of truth. Do not reproduce package-manager operations
inside the model or mutate the environment that currently hosts an MCP process.

## Complete first-time setup

Prefer the complete workflow when the user wants Agent Eyes ready in an MCP
client:

```bash
uv --version
uvx agent-eyes@latest setup --dry-run
uvx agent-eyes@latest setup
agent-eyes --version
agent-eyes doctor --verbose
```

The `uvx` environment only runs the latest bootstrap wizard. Setup installs the
matching platform package persistently, configures selected MCP clients, installs
the canonical skill for Claude Code/Codex plus Codex metadata, and verifies the
persistent providers. Restart every client reported as changed.

On Linux, require `/usr/bin/python3` 3.10 or newer, `pipx`, and distro
AT-SPI/PyGObject packages before setup. The persistent runtime uses
`pipx --system-site-packages --python /usr/bin/python3` so it can import the
distro bindings. Report missing prerequisites exactly as the CLI reports them;
do not run `sudo` automatically.

## Runtime-only install or repair

Use this only when the user explicitly wants the persistent executable without
MCP client configuration:

```bash
uvx agent-eyes@latest install --dry-run
uvx agent-eyes@latest install
agent-eyes doctor --verbose
```

On an already installed current launcher, the equivalent commands begin with
`agent-eyes install`. A healthy current launcher is a no-op. Use
`agent-eyes install --repair` for a forced same-version runtime reinstall. Use
the complete `uvx agent-eyes@latest setup` flow for upgrades so client entries
and skills are synchronized too.

`agent-eyes install` does not configure MCP clients or install skills. Use
`agent-eyes init` afterward, or use complete setup instead.

## Safety and reporting

- Never run `pip install` inside an active MCP/uvx environment.
- Never silently run `sudo`, grant OS permissions, or approve browser dialogs.
- Never treat an installed Boolean as readiness; use the live doctor result.
- Treat `~/.agent-eyes/readiness.json` as a cache, not proof.
- Do not require Chrome remote debugging or Playwright MCP for foreground use.
- Report the installed version, executable path, readiness state, remediation,
  and clients that still need a restart.
