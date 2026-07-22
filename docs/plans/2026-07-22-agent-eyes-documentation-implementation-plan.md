# Agent Eyes Documentation and Contract Alignment Implementation Plan

**Status:** In progress

**Approved design:** [Install-to-ready and MCP reference documentation](2026-07-22-agent-eyes-documentation-design.md)

**Base:** `main` at v0.9.0 (`8b1a665`)

**Branch:** `codex/docs-agent-eyes-reference`

## 1. Readiness and Audit Summary

The repository is a Python 3.10+ CLI/MCP package with a setuptools build and uv
development environment ([`pyproject.toml`](../../pyproject.toml)). The executable
surface is defined by:

- CLI parser and handlers: [`src/agent_eyes/cli.py`](../../src/agent_eyes/cli.py)
- MCP schemas and handlers: [`src/agent_eyes/server.py`](../../src/agent_eyes/server.py)
- Dynamic schema constraints: [`src/agent_eyes/tool_contract.py`](../../src/agent_eyes/tool_contract.py)
- Client discovery/configuration: [`src/agent_eyes/setup/scanner.py`](../../src/agent_eyes/setup/scanner.py)
- Canonical agent workflow: [`skills/agent-eyes/SKILL.md`](../../skills/agent-eyes/SKILL.md)

The current README is accurate about the setup consent boundary, native-first
browser policy, readiness states, event-driven waits, platform model, and v0.9.0
benchmarks. It is incomplete as an install-to-ready guide and exhaustive CLI/MCP
reference.

## 2. Files in Scope

### Documentation

- `README.md`
- `docs/api/agent-eyes-cli.md` (new)
- `docs/api/mcp-tools.md` (new)
- `skills/agent-eyes/SKILL.md`
- `skills/install/SKILL.md`
- `skills/init/SKILL.md`
- `src/agent_eyes/setup/templates/skill.py`

### Public contract implementation

- `src/agent_eyes/cli.py`
- `src/agent_eyes/server.py`
- `src/agent_eyes/tool_contract.py`
- `src/agent_eyes/input_validation.py`
- `src/agent_eyes/setup/templates/mcp_entry.py`

### Tests

- `tests/test_cli.py`
- `tests/test_tool_contract.py`
- `tests/test_server_input_safety.py`
- `tests/test_event_wait.py`
- `tests/test_browser_foreground_policy.py`
- `tests/test_input_validation.py`
- `tests/test_readme_commands.py`
- `tests/test_documentation_contract.py` (new)
- `tests/test_smoke_installed_artifact.py`
- `scripts/smoke_installed_artifact.py`

### Cleanup

- Update `.gitignore` and remove the unreferenced `.playwright-mcp` blank
  screenshot artifact. Preserve
  Playwright competitor-detection code and its tests because that code identifies
  conflicting MCP configurations; it is not a runtime dependency.
- Ignore `.playwright-mcp/` so local Playwright MCP artifacts cannot be committed
  accidentally again.

## 3. Phase 1 — Contract Regression Tests (RED)

Add focused tests before changing behavior:

1. `setup --client unknown` returns exit code `2`, matching `init` usage errors.
2. Compatibility flag help states truthful behavior:
   - doctor is always a live probe;
   - install's profile does not alter the platform package;
   - `--all-detected` explicitly selects the default set.
3. Effective schemas encode runtime limits and combinations:
   - `web_tree.max_depth <= 10`;
   - `subtree.max_depth <= 15`;
   - `find` requires a filter and PID or snapshot;
   - `click` requires element identity or complete PID-guarded coordinates;
   - `wait` requires a condition and exactly one foreground PID or shadow target;
   - `close_tab` requires foreground target/title or shadow target;
   - `hover` requires element identity or complete coordinates;
   - window mutations require exact snapshot-qualified targets and geometry.
4. Foreground wait missing PID and timeout results set MCP `isError=true`.
5. The Apple Events compatibility `shadow` tool is absent off macOS.
6. Documentation coverage test fails until every CLI option and effective MCP
   tool/property is represented in the appropriate reference.

Gate: every new test fails for the intended current behavior or missing artifact.

## 4. Phase 2 — Minimal Contract Alignment (GREEN)

### CLI

1. Separate `ValueError` from setup/dependency failures in `_run_setup` and map
   invalid client selection to `ExitCode.USAGE`.
2. Preserve accepted flags for compatibility but replace misleading help text
   with exact semantics.
3. Keep `--all-detected` as an explicit, script-readable alias for default
   detection; do not remove it.

### MCP

1. Move per-tool limits/conditional schemas into the effective schema contract
   without loosening global hardening.
2. Align tree-depth maximums with handler caps.
3. Add JSON Schema `anyOf`/`allOf`/`not` conditions matching the handler's
   accepted argument combinations and rejecting ambiguous mixed modes.
4. Correct descriptions where runtime behavior is intentional:
   - coordinate clicks require PID focus verification before OS input;
   - `max_results` applies with or without a query;
   - upload paths are canonicalized before protected-root checks.
5. Prefix wait timeout/missing-target failures with `ERROR:` so the central MCP
   result wrapper sets `isError=true`.
6. Filter the macOS-only `shadow` compatibility tool from non-macOS catalogs and
   update platform tool-count assertions.

Gate: focused CLI, tool-contract, input-safety, wait, and platform tests pass.

## 5. Phase 3 — README and Skill Synchronization

Rewrite the README using progressive disclosure:

1. Prerequisites and platform-specific bootstrap requirements.
2. Canonical latest-version setup command and dry-run.
3. What setup installs/configures and what it deliberately does not do.
4. Version/readiness verification and expected status.
5. Client restart and first MCP call.
6. Supported platform/provider and client/config tables.
7. Foreground and explicit-shadow workflows.
8. Common CLI syntax with exhaustive-reference links.
9. Readiness, troubleshooting, upgrade, repair, removal, development, and
   benchmark sections.

Synchronize all three repository skills with the same `@latest` bootstrap and
truthful install/init behavior. Do not add model-specific setup logic.

Gate: README commands, skill synchronization, and setup/configurator tests pass.

## 6. Phase 4 — Exhaustive CLI Reference

Create `docs/api/agent-eyes-cli.md` with:

- version/runtime/platform compatibility;
- top-level grammar, help, version, and no-command alias;
- all five subcommands;
- every option, default, allowed value, combination, output, side effect,
  failure, and copyable example;
- machine-readable JSON fields;
- exit codes `0` through `5`;
- interactive/non-interactive behavior;
- all ten supported `--client` IDs and their config/skill artifacts;
- state and launcher environment overrides;
- stdout/stderr and stdio constraints;
- upgrade, repair, and removal syntax.

Gate: parser-derived documentation coverage and every CLI help invocation pass.

## 7. Phase 5 — Exhaustive MCP Reference

Create `docs/api/mcp-tools.md` with cross-cutting contracts followed by one
uniform entry for every effective tool:

1. `status`
2. `list_apps`
3. `tree`
4. `find`
5. `click`
6. `type`
7. `focused`
8. `list_tabs`
9. `web_tree`
10. `navigate`
11. `js`
12. `press_key`
13. `wait`
14. `new_tab`
15. `close_tab`
16. `dialog`
17. `upload`
18. `scroll`
19. `drag`
20. `fill_form`
21. `hover`
22. `app` (macOS)
23. `subtree`
24. `window` (macOS)
25. `context`
26. `shadow` (macOS compatibility)
27. `pierce`
28. `install_check`

Each entry documents purpose, availability, parameters, result, errors, side
effects, safety, and a schema-valid JSON input. Cross-cutting sections document
stdio/tools-only MCP behavior, provider routing, snapshots, target IDs, input
and output bounds, redaction, mutation ordering, and uncertain outcomes.

Gate: every live tool/property is covered, every example validates against its
effective schema, and no zombie tool is documented for the tested platform.

## 8. Phase 6 — Full Verification and Fix Loop

Run in order and fix every task-owned failure before continuing:

1. `git diff --check`
2. `uvx ruff check src benchmarks scripts`
3. `uvx bandit -r src scripts -q -ll`
4. Focused CLI/MCP/documentation suites
5. Complete `uv run python -m pytest -q`
6. `uv build`
7. Package metadata and artifact-content inspection
8. Installed-wheel CLI/MCP/setup smoke using `scripts/smoke_installed_artifact.py`
9. Root and subcommand help/version checks from the installed artifact
10. Published bootstrap dry-run outside the repository

The generic quality-gates skill does not recognize Python projects, so this
repository's checked-in Python lint, security, test, build, and installed-artifact
commands are the authoritative fallback.

## 9. Completion Criteria

- README alone takes a new user from prerequisites to a recognized ready state.
- CLI and MCP references exhaustively match their executable contracts.
- Every example is schema-valid and every local link resolves.
- Canonical Agent Eyes skills describe the same setup and runtime policy.
- The accidental `.playwright-mcp` screenshot is removed without deleting
  competitor detection.
- Focused and full verification gates pass from source and built artifacts.
- The branch contains no unrelated modifications.
