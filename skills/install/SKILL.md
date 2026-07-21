---
name: install
description: >
  Guided Agent Eyes runtime installation and repair. Use for first-time setup,
  missing native/input dependencies, permission diagnosis, or upgrades. Delegates
  to the model-independent agent-eyes CLI and never installs packages inside the
  MCP server process.
---

# Agent Eyes install

The CLI is the source of truth. Do not reproduce package-manager logic in the model.

On a fresh machine, use `uvx` only for the bootstrap commands below; after setup,
the MCP and all normal checks must use the persistent `agent-eyes` launcher.

1. Run the read-only diagnosis (use the fallback if the persistent launcher is not installed yet):

   ```bash
   agent-eyes doctor --verbose
   ```

   If that executable is not available, run `uvx agent-eyes doctor --verbose`.

2. Preview the exact user-level installation plan:

   ```bash
   agent-eyes install --dry-run
   ```

3. Run the guided installer:

   ```bash
   agent-eyes install
   ```

   A healthy current persistent launcher is a no-op. Use `agent-eyes install --repair` only to force a reinstall.

4. Follow any OS permission or Linux system-package instruction printed by the CLI, then rerun doctor.

For a complete first run—including persistent install, MCP client initialization, and verification—prefer:

```bash
uvx agent-eyes setup
```

Rules:

- Never run `pip install` against the active MCP/uvx environment.
- Never silently use sudo or approve OS/browser dialogs.
- Never write an installed Boolean; readiness comes from live checks and `~/.agent-eyes/readiness.json` is only a cache.
- Chrome remote debugging is optional shadow capability and is never a normal installation requirement.
- Report the exact CLI status and remediation; do not claim readiness from command exit alone.
