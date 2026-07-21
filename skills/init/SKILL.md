---
name: init
description: >
  Safely configure Agent Eyes in detected MCP clients after runtime installation.
  Uses the model-independent CLI, persistent absolute launcher paths, previews,
  backups, atomic writes, and live verification. Keeps existing MCP servers by default.
---

# Agent Eyes init

Use the CLI rather than editing MCP files from model-generated instructions.

1. Verify runtime readiness:

   ```bash
   agent-eyes doctor --verbose
   ```

2. Preview detected client changes:

   ```bash
   agent-eyes init --dry-run
   ```

3. Apply the reviewed configuration:

   ```bash
   agent-eyes init
   ```

Configure one client when requested:

```bash
agent-eyes init --client cursor
```

The initializer must:

- use an absolute persistent `agent-eyes` executable with `args: ["serve"]`;
- synchronize the canonical Agent Eyes skill for selected clients that support skills;
- preserve unrelated config and all competing MCP servers unless the user separately asks to remove them;
- preserve supported JSON, JSONC, and TOML formats and reject malformed input rather than replacing it;
- preflight every selected config and skill artifact, then apply them as one rollback-capable transaction;
- create a backup for every existing changed artifact;
- be idempotent;
- tell the user which clients need a restart/reload.

If the persistent launcher or a required provider is missing, run `agent-eyes setup` rather than attempting an inline install.
