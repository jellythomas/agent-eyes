# Agent Eyes v2 Bootstrap: Implementation Plan

**Date:** 2026-07-17
**Design:** `docs/plans/2026-07-17-agent-eyes-v2-native-first-design.md`
**Phase:** Slice 1 — implemented and verified; retained as an implementation record

## Summary

Replace runtime self-installation and stale boolean setup checks with a model-independent CLI, live capability probes, a versioned atomic readiness manifest, truthful MCP initialization/status output, safe persistent client configuration, and complete README guidance.

## Scope Verification

| Requirement | Delivery |
|---|---|
| Check dependencies when Agent Eyes first attaches | Fast read-only probe during MCP construction; full `doctor` from CLI |
| Guide installation | `agent-eyes setup` prints a plan, asks once, and resumes verification |
| Install required dependencies | Persistent `uv tool install` path, pipx fallback, current-platform packaging markers |
| Never silently mutate runtime | Remove `_auto_install_platform_deps` from MCP startup |
| Work without model-specific slash commands | CLI is canonical; slash skills delegate to it |
| Keep MCP available when unprepared | Core server starts and reports `setup_required` with one recovery command |
| Avoid Chrome-specific normal setup | CDP is reported as optional/explicit shadow capability |
| Configure persistent MCP launcher | Generated entries use a resolved absolute `agent-eyes` executable |
| Preserve user configuration | Fail closed on invalid JSON, unique backup, atomic replace |
| Guide users in README | Tested quick-start, commands, state meanings, platform/permission troubleshooting |

## Historical Starting Points

The original audit found a direct server console entry, runtime dependency installation, optimistic status, legacy Boolean state, non-atomic writes, and permanent `uvx` client entries. These were pre-implementation findings and are no longer claims about the current tree. The delivered code is the source of truth; implementation details below use the actual module names.

## Implementation Steps

### 1. Write readiness and CLI contract tests (RED)

Create:

- `tests/test_readiness.py`
- `tests/test_cli.py`
- `tests/test_setup_configurator.py`

Cover:

1. Empty state: missing manifest and missing required provider.
2. Happy path: native adapter and input provider are available and permitted.
3. Boundaries: optional capability missing produces `degraded`; required dependency missing produces `setup_required`; permission denial produces `permission_required`.
4. Error handling: corrupt manifest/config, failed install command, interrupted config write.
5. Integration: CLI setup sequence and server status/instructions use the same report.
6. Feature flags: not applicable; explicit shadow mode remains a capability, not a setup flag.

### 2. Introduce the canonical CLI

Create `src/agent_eyes/cli.py` with an `argparse` command surface:

- no subcommand -> lazy import and call `serve`;
- `serve` -> lazy import `agent_eyes.server.main`;
- `doctor` -> run live probes, optionally emit JSON, persist the verified snapshot;
- `install` -> create/preview/apply an installation plan;
- `init` -> preview/apply safe client configuration;
- `setup` -> compose install, permissions guidance, init, and verification.

Modify `pyproject.toml` to point `agent-eyes` at `agent_eyes.cli:main`. Keep server imports out of CLI module scope so `doctor` and setup do not initialize the MCP runtime.

Verified POC: Python 3.12.11 successfully parsed no-command -> `serve`, `doctor --json`, and `setup --yes`; an atomic temp-file + `os.replace` state write also passed on this workspace.

### 3. Add live readiness reports and atomic state

Create `src/agent_eyes/setup/readiness.py`:

- immutable capability/report data models;
- platform adapter loader without package installation;
- input/native permission probes;
- persistent executable/environment probe;
- deterministic overall-state derivation;
- compact text and JSON serialization.

Refactor `src/agent_eyes/setup/state.py`:

- one schema-versioned manifest;
- atomic write in the state directory;
- process lock for concurrent setup invocations;
- fingerprint invalidation on schema/version/platform/architecture/Python/executable changes;
- compatibility reader for legacy `state.json`, without trusting its booleans as readiness.

### 4. Build explicit installation planning

Create `src/agent_eyes/setup/install.py`:

- use `uv tool` on macOS/Windows and a system-site-capable `pipx` path on Linux, otherwise return an actionable unsupported-manager result;
- pin the persistent installation to the currently running Agent Eyes version;
- represent commands as argument arrays, never shell strings;
- preview every mutation before executing;
- never install from MCP import/startup;
- never auto-elevate or use `--break-system-packages`;
- return resumable remediation for Linux system packages and OS/browser permission steps.

Modify `pyproject.toml` so pure-Python platform bindings use platform markers in normal dependencies. Keep heavy visual/shadow/dev dependencies optional.

### 5. Make MCP startup launch-safe and truthful

Modify `src/agent_eyes/server.py`:

- delete runtime package installation;
- load the adapter once without mutation;
- initialize `Server` with current version and one readiness instruction;
- replace the first-tool reminder with readiness-aware tool results;
- replace `install_check` with a compatibility alias backed by live readiness;
- make status derive `ready`, `degraded`, `setup_required`, or `permission_required` from capabilities;
- keep all diagnostics on stderr.

Add focused tests that importing/starting the server never calls a package manager and that unavailable providers do not prevent tool listing/status.

### 6. Make MCP client configuration safe and persistent

Modify:

- `src/agent_eyes/setup/templates/mcp_entry.py`
- `src/agent_eyes/setup/configurator.py`
- `src/agent_eyes/setup/scanner.py` only if client metadata needs an explicit scope/path fix.

Behavior:

- resolve the persistent executable to an absolute path;
- preview selected client config changes;
- default to keeping all other MCP servers and skills;
- reject invalid existing JSON without overwriting it;
- create collision-proof backups;
- write via atomic replace;
- retain an idempotent no-change result.

### 7. Make skills thin wrappers

Modify `skills/install/SKILL.md` and `skills/init/SKILL.md` so they invoke and explain the canonical CLI. Remove duplicated package-manager logic, hard-coded versions, `install.json` trust, and Chrome debug-port requirements from the normal path.

### 8. Rewrite and verify README onboarding

Modify `README.md` with:

- one-command first setup;
- what will and will not be installed;
- approval/permission boundaries;
- persistent MCP configuration examples;
- `doctor`, `repair`/rerun, init, upgrade, uninstall limitations;
- readiness states and JSON diagnostics;
- platform dependency/permission table;
- browser reuse and explicit-shadow behavior;
- no normal Chrome remote-debugging requirement;
- development setup distinct from end-user setup.

Add a documentation test that extracts/validates the primary command examples and generated MCP entry.

### 9. Verify the slice

Run in order:

1. Focused new tests.
2. Existing full pytest suite.
3. Available formatter/linter/type checks declared by this repository.
4. `uv lock --check` and wheel build.
5. CLI smoke tests for help, doctor text/JSON, dry/declined install, and no-argument compatibility.
6. Manual MCP initialize/list-tools/status exchange.
7. Benchmark import, doctor, and cached readiness overhead; report measured values only.

## Quality Checklist

- [x] No package manager or network work occurs during `serve` startup.
- [x] No stdout diagnostics can corrupt stdio MCP.
- [x] Required and optional capabilities have distinct states.
- [x] Every mutation requires explicit CLI invocation and preview/consent.
- [x] Invalid config is never overwritten; every selected config is preflighted before writes.
- [x] State/config writes are atomic and readiness updates use a process lock.
- [x] Setup is idempotent, skips healthy reinstall work, and supports explicit repair.
- [x] Existing MCP configurations remain compatible through no-argument `serve`.
- [x] README examples match tested behavior.
- [x] New and existing tests pass on the development host; cross-platform CI remains a release gate.

## Key Decisions

- CLI over slash commands: MCP clients and models do not share a slash-command system.
- Persistent uv/pipx tool over permanent uvx: native-host and per-user service paths must remain stable.
- Probe-derived readiness over installed booleans: permissions and providers can drift after installation.
- Foreground/native core over browser remote protocols: normal operation must reuse the user's open browser without a debug-port restart.
- Incremental delivery: bootstrap correctness precedes the broker/bridge migration, but public contracts already match the v2 architecture.

## Assumptions

- Published releases continue to expose the `agent-eyes` distribution and executable names.
- Python 3.10 remains the minimum declared runtime (`pyproject.toml:7`).
- Platform-specific signed bundles are a later distribution layer; Slice 1 remains Python/uv/pipx based.
- Browser bridges are optional in Slice 1 and become recommended once prebuilt cross-browser artifacts exist.

## Concerns

- Linux PyGObject/AT-SPI requires distro-specific packages; setup must guide or explicitly execute a reviewed package-manager plan rather than mutate system Python.
- macOS Accessibility permission belongs to the responsible launcher/client process, not always Terminal.
- A freshly installed uv tool may live outside the current process PATH; init must resolve uv's tool bin directory directly.
- Current setup code can modify competitors and Claude instructions. The new default path must not call those destructive behaviors.
