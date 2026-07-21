# Agent Eyes Production Hardening: Implementation Plan

**Date:** 2026-07-17
**Design:** `docs/plans/2026-07-17-agent-eyes-production-hardening-design.md`
**Status:** Approved for autonomous execution under the user's ongoing-goal authorization
**Target release:** `0.9.0` (provisional until the artifact gate)
**Delivery rule:** regression first, one bounded slice at a time, no local reinstall before every pre-install gate passes

## 1. Summary

Agent Eyes already exposes a native-first MCP surface, but its runtime still combines concurrent MCP requests with module-level mutable registries, stateful adapters, index/URL-based browser routing, incomplete shadow consent, unbounded blocking work, and inconsistent setup/skill delivery. The result can be slow, ambiguous, or wrong-target even when individual unit tests pass.

This plan hardens the current implementation behind its existing tool names. It introduces a broker-ready in-process coordinator, immutable observation snapshots, exact native and shadow target identities, fail-closed action routing, one monotonic operation deadline, event-loop-safe provider work, strict input/output budgets, truthful cross-platform capabilities, and a reproducible setup/package/skill pipeline. Only after the source, security, stress, artifact, and live-computer gates pass will the exact local Agent Eyes tool be removed and replaced with the newly built wheel.

No project-level `AGENTS.md` exists in this repository; the supplied global instructions therefore govern the work. Searches found no existing `AutomationCoordinator`, `ObservationStore`, `OperationBudget`, or `ResultFormatter` implementation. The implementation will add those boundaries without prematurely extracting every existing provider into a new hierarchy.

## 2. Source-of-Truth Findings

The following verified behaviors define the starting point and the required regression surface:

| Finding | Current evidence | Required outcome |
|---|---|---|
| MCP state is global | `registry`, adapter, input backend, CDP client, tier manager, and pool are initialized at module scope in `src/agent_eyes/server.py:65-99` | Request lifecycle state belongs to one coordinator; observations are immutable and actions are serialized by target/mode |
| A tree replaces all prior IDs | `ElementRegistry.register_tree()` clears the registry at `src/agent_eyes/registry.py:29-42` | IDs resolve only with their originating snapshot, provider, target, and generation |
| Tool calls can interleave | `call_tool` dispatches directly at `src/agent_eyes/server.py:946-1016`; MCP 1.26 runs messages concurrently | Seeded parallel schedules produce zero wrong-target actions |
| Native target validation omits window identity | `_browser_target_is_active()` starts at `src/agent_eyes/server.py:1291` | Exact owning window and tab are raised and reverified immediately before input |
| Native destructive actions can use stale cached objects | `_handle_close_tab()` starts at `src/agent_eyes/server.py:2431` | Fresh inventory must resolve the supplied current handle; stale or ambiguous targets fail before mutation |
| Shadow targeting uses mutable metadata | `_get_cdp_session()` starts at `src/agent_eyes/server.py:1616`; legacy tab resolution begins at `src/agent_eyes/server.py:2035` | CDP target ID or BiDi browsing-context ID is canonical; URL/title/index are display metadata only |
| Persistent and legacy tab flows coexist | `_ensure_tabs()` and `_get_tab()` start at `src/agent_eyes/server.py:1977` and `src/agent_eyes/server.py:2002` | One typed shadow target/session map; no cross-provider cache reuse |
| Click/type do not declare shadow consent | Schemas begin at `src/agent_eyes/server.py:213` and `src/agent_eyes/server.py:243`; handlers begin at `src/agent_eyes/server.py:1324` and `src/agent_eyes/server.py:1405` | Every action-capable tool validates an explicit mode and rejects cross-mode handles |
| Native wait queries can exceed their timeout | `wait_for_native_element()` begins at `src/agent_eyes/native_events.py:843` | One absolute deadline bounds subscription, query, action, fallback, and cleanup |
| Native typing can block the event loop | `_handle_type()` begins at `src/agent_eyes/server.py:1405`; input backends begin at `src/agent_eyes/input_sim.py:36` | Provider work runs off-loop on a bounded worker; bulk text is the fast default where supported |
| Caller data is embedded into generated programs | AppleScript shadow helpers start at `src/agent_eyes/applescript.py:467`, including selectors at `src/agent_eyes/applescript.py:493` and keys at `src/agent_eyes/applescript.py:531` | Caller values are serialized as data and verified with hostile-string tests |
| Shadow DOM references are not consistently actionable | `_append_shadow_dom_elements()` starts at `src/agent_eyes/server.py:1777`; `merge_pierced_nodes()` is in `src/agent_eyes/js_bridge.py:160`; `_handle_pierce()` starts at `src/agent_eyes/server.py:3164` | Returned actionable nodes carry the identifier consumed by the action path; selector filtering changes results |
| Generic app/window surface is not truthful cross-platform | `_handle_app()` and `_handle_window()` start at `src/agent_eyes/server.py:2879` and `src/agent_eyes/server.py:2965` | Tool availability and doctor output share one live capability registry |
| Packaging declares broad support | Python 3.10-3.13 and three OS families are declared in `pyproject.toml:5-24`; console entry is `agent_eyes.cli:main` at `pyproject.toml:54-55` | CI and installed-artifact smoke cover the declared matrix; unsupported live capabilities are reported explicitly |
| Existing setup is JSON-oriented and skill delivery is split | CLI config flow starts at `src/agent_eyes/cli.py:387`; generated skill logic remains in `src/agent_eyes/setup/configurator.py:667` | Codex TOML and JSON clients use safe adapters; one canonical skill contract produces client-valid copies |

## 3. Scope Verification

| User requirement | Planned delivery | Final proof |
|---|---|---|
| Inspect already-open tabs first | Native inventory is the default orientation path; query ranks every visible supported browser target | Live Chrome and Firefox fixture plus YouTube existing-tab trace |
| Open a new tab only when needed | Ranked safe-match policy; new-tab path is allowed but never the first blind action | Existing-tab and no-match trace assertions |
| Do not depend on Chrome remote mode | Native OS accessibility is the foreground core; remote protocols remain explicit shadow providers | Zero implicit `shadow=true` calls in common journeys |
| Work with browsers other than Chrome | Cross-browser native inventory and activation contract; optional bridge design remains shared-core/per-browser-package | Chrome and Firefox live loopback fixture; capability matrix for Safari/Edge/others |
| Avoid fixed orchestration sleeps | Condition subscription before dispatch; adaptive bounded fallback only when events are unavailable | Immediate-event latency and timeout-precision benchmarks |
| Improve execution time | Single-flight observations, revision cache, bounded worker lanes, compact deltas, lazy imports | Versioned cold/warm p50/p95/max benchmarks |
| Improve stability and wrong-target safety | Snapshot-qualified handles, exact window/tab validation, mutation serialization, fail-closed stale handling | 1,000 seeded schedules and 100 repeated stress runs |
| Minimize model context usage | Short snapshot token once per response, local integer IDs, ranked/limited results, global byte budget | Common journey <=12 KiB; default result <=16 KiB |
| Check/install dependencies on first setup | Read-only readiness at attach; explicit preview/consent via install/setup; exact missing dependency remediation | Clean-environment CLI and artifact tests |
| Guided README | One tested path for setup, repair, init, reconnect, shadow mode, and uninstall/rollback | Documentation command tests and clean-install walkthrough |
| Repair installed skills | One semantic contract, Codex-valid frontmatter, tool-schema synchronization, installed-copy verification | Skill validator plus executable trace evaluations |
| Clean and reinstall local MCP | Exact-target uninstall only after green artifact gates; fresh wheel installed by absolute path; clients reconnected | Version/entrypoint/MCP handshake/config/live smoke from installed artifact |
| Keep iterating until flawless release gates | Audit, fix, rerun; require three consecutive complete green passes and no unresolved P0/P1/P2 | Saved reports for each full pass |

## 4. Delivery Invariants

These are implementation and review gates, not aspirational guidelines:

1. A bare integer element ID is never globally authoritative.
2. Every actionable record is bound to snapshot, provider, mode, target identity, and generation.
3. Foreground tools cannot invoke a shadow provider without explicit shadow intent; shadow tools cannot fall back to physical input.
4. URL, title, window index, and tab index are never mutation identities.
5. Destructive actions require an explicit current target; no implicit tab zero.
6. Process focus alone is insufficient: the exact owning window and selected tab/element must be verified before input.
7. A mutation is dispatched at most once unless the provider proves it was never transmitted.
8. Ambiguity, stale state, disconnect, unverifiable focus, and deadline exhaustion fail closed.
9. Every blocking call consumes the same operation deadline and cannot block the event loop.
10. Every response path has item and byte limits with explicit truncation metadata.
11. Typed values, uploaded content, JavaScript results, and other secrets never appear in logs or success echoes.
12. Caller-controlled values are encoded as data, never interpolated into executable source.
13. Existing public tool names remain available; incompatible unsafe defaults are removed rather than silently preserved.
14. The foreground core remains browser-neutral. An extension bridge is optional enrichment and never a prerequisite.

## 5. Ordered Implementation Plan

### Phase 0 — Freeze evidence and protect the workspace

**Purpose:** preserve the user's dirty worktree and make every later result attributable.

Actions:

1. Record `git status --short`, HEAD, Python/uv/MCP versions, exact local executable resolution, installed uv-tool record, and current client entry for Agent Eyes only.
2. Save the existing benchmark measurements and test count as a versioned baseline artifact under `benchmarks/results/`; redact home-specific or secret data.
3. Run the current full suite once before new RED tests. Record failures without altering unrelated files.
4. Confirm the prospective implementation files do not already exist with `rg --files` and exact-symbol searches.
5. Do not uninstall, overwrite configs, replace global skills, terminate clients, or rebuild the published-version wheel in this phase.

Gate:

- Baseline is reproducible and the exact pre-existing dirty paths are documented.
- Any pre-existing failing test is distinguished from a newly introduced RED regression.

### Phase 1 — Add release-blocking regression tests (RED)

**Existing fixtures to extend:** `tests/test_registry_v2.py`, `tests/test_close_tab.py`, `tests/test_native_events.py`, `tests/test_cdp_persistent.py`, `tests/test_cdp_reconnect.py`, `tests/test_shadow_dom.py`, `tests/test_browser_foreground_policy.py`, `tests/test_setup_configurator.py`, `tests/test_readiness.py`, and `tests/test_response_format.py`.

**New focused files:**

- `tests/test_observation_store.py`
- `tests/test_coordinator_concurrency.py`
- `tests/test_operation_budget.py`
- `tests/test_action_mode_consent.py`
- `tests/test_shadow_target_identity.py`
- `tests/test_output_security.py`
- `tests/test_setup_paths.py`
- `tests/test_skill_contract.py`

Reusable deterministic fakes are centralized in `tests/support/fake_providers.py` and `tests/support/schedules.py`; race tests use events/barriers and action ledgers rather than timing sleeps.

Required RED cases:

1. Concurrent native trees return distinct snapshot-qualified IDs; an action from response A cannot resolve into response B.
2. Two windows in one browser, duplicate titles/URLs, reorder, navigation, PID reuse, and replacement never redirect an action.
3. Stale native target use leaves activation and input spies untouched.
4. Duplicate shadow URLs, attach/detach, reorder, and reconnect route only by target ID and current session generation.
5. Every generic action rejects a shadow record unless explicit shadow mode is present.
6. `close_tab` without an explicit current target fails closed.
7. Slow observer registration, native query, input, and cleanup respect the total deadline.
8. A 10 ms heartbeat remains responsive during slow mocked provider work.
9. A million-character JS result, huge trees/tabs/apps, and huge exception messages respect the response ceiling.
10. Sentinel secrets in type/fill/upload inputs are absent from logs and results.
11. Quotes, backslashes, newlines, U+2028, and U+2029 in selector/text/role/key inputs remain data.
12. Shadow DOM selector filtering is honored and returned actionable records use the correct backend node identity.
13. Windows APPDATA and Linux XDG paths, symlinks, JSONC, TOML, concurrent setup, and backup injection are deterministic and isolated to temp paths.
14. Tool schema, repository skill, generated skill, and installed-client variants expose the same real tool/mode contract.

Gate:

- Each regression fails for the intended reason on the starting implementation.
- Tests use fakes/mocks and do not mutate the user's real UI, configs, skills, backup directory, or installed package.

### Phase 2 — Implement runtime primitives in isolation (GREEN)

Create the smallest flat modules that match this repository's existing package organization:

- `src/agent_eyes/operation.py`
- `src/agent_eyes/observations.py`
- `src/agent_eyes/result_format.py`
- `src/agent_eyes/coordinator.py`

Responsibilities:

1. `operation.py`: operation mode/context, compact stable error codes, and one absolute monotonic budget with remaining/checkpoint/bounded-await behavior. It never starts a fresh timeout per layer.
2. `observations.py`: immutable snapshot and element records; compact unguessable process-local tokens; bounded count/age; release hooks; exact provider/mode/target/generation checks; legacy ID-only resolution only when exactly one live snapshot is unambiguous.
3. `result_format.py`: normalize text, enforce item/default/hard byte limits, sanitize errors, attach deterministic truncation metadata, and keep secret-bearing arguments out of formatting/logging.
4. `coordinator.py`: dependency-injected providers, one foreground mutation lane, per-shadow-target ordering, single-flight identical observations, operation budgets, and clean shutdown.

Implementation constraints:

- Use immutable dataclasses or equivalent frozen records for identity-bearing values.
- Use the running loop's monotonic clock; tests inject a clock only where deterministic expiry is required.
- Keep provider calls behind verified callables/protocols; do not import platform frameworks from these modules.
- Do not add a daemon, database, network listener, or extension dependency.

Gate:

- 100% line and branch coverage for these new modules.
- 1,000 randomized in-process schedules produce zero wrong-target resolution.
- Coordinator validation overhead p95 <=5 ms.
- 10,000 snapshot create/invalidate cycles grow RSS by no more than max(10 MiB, 5%).

### Phase 3 — Integrate the coordinator behind the MCP transport

Modify `src/agent_eyes/server.py` around its module initialization (`src/agent_eyes/server.py:65-99`), tool dispatcher (`src/agent_eyes/server.py:940-1030`), and every observation/action handler.

Actions:

1. Construct one runtime context/coordinator for the MCP server lifecycle; handlers receive/use it rather than owning mutable globals.
2. Keep `TOOLS` names stable, add compact `snapshot` and explicit `shadow` arguments wherever an action can consume an observed record, and align runtime validation with JSON schemas.
3. Make tree/find/subtree/focused/context/web-tree/pierce observations create or derive immutable snapshots without clearing unrelated state.
4. Route click/type/press-key/scroll/drag/fill/hover/navigate/dialog/upload/close actions through coordinator resolution before any provider call.
5. Retain an ID-only compatibility path for one release only when one live unambiguous snapshot can prove ownership; otherwise return a typed migration error.
6. Bound and format every `call_tool` result centrally so individual handlers cannot bypass the hard result ceiling.

Gate:

- MCP initialize, tool-list, and all tool schemas work through an in-memory JSON-RPC test.
- Concurrent calls cannot bypass coordinator ordering.
- Existing public names remain; schema/runtime invariants match exactly.
- MCP stdout contains protocol traffic only.

### Phase 4 — Make native observations and actions exact

Modify:

- `src/agent_eyes/adapters/base.py`
- `src/agent_eyes/adapters/macos.py`
- `src/agent_eyes/adapters/windows.py`
- `src/agent_eyes/adapters/linux.py`
- `src/agent_eyes/browser_inventory.py`
- native branches in `src/agent_eyes/server.py`

Actions:

1. Move adapter traversal counters/flags into request-local traversal state, or serialize the current adapters on one provider-owned worker until every platform implementation is stateless.
2. Extend browser target identity with opaque process, owning-window, and tab references. Keep indices, URL, and title only for display/ranking.
3. Implement the mutation sequence: fresh inventory resolution -> raise exact window -> select exact tab/element -> verify exact window and selection -> dispatch once -> await completion.
4. Replace stale cache lookup in close/navigate/actions with fresh exact-handle resolution.
5. Serialize physical input across foreground tools and prevent a late timed-out worker from overlapping a newer foreground mutation.
6. Preserve the current native-first all-browser inventory and ranking behavior while adding revision invalidation and identical-call single-flight.

Platform contract tests:

- macOS AX: owning-window reference, frontmost verification, exact tab selection.
- Windows UIA: owning top-level window and tab element identity.
- Linux AT-SPI: owning application/window path and explicit degraded behavior when the compositor denies input.

Gate:

- Zero wrong-target actions in 1,000 seeded schedules.
- 32 identical inventory calls invoke the provider once and finish within 1.25x one scan.
- Native `list_tabs`: cold p95 <=300 ms, warm p95 <=50 ms on the reference host.
- Exact-target failures invoke no input method.

### Phase 5 — Split and harden explicit shadow providers

Modify:

- `src/agent_eyes/cdp_persistent.py`
- `src/agent_eyes/cdp.py`
- `src/agent_eyes/js_bridge.py`
- `src/agent_eyes/applescript.py`
- shadow branches in `src/agent_eyes/server.py`

Actions:

1. Make CDP `targetId` the canonical target key and bind session/document generations beneath it.
2. On reconnect, resolve the same target ID to the newly attached session object; clear old futures, handlers, and session mappings.
3. Retry only verified idempotent reads after a proven pre-dispatch disconnect. Never generically replay a mutation after an uncertain socket failure.
4. Remove shared persistent/legacy tab caches and all first-equal URL/title/index mutation routing.
5. Require explicit shadow intent on every shadow-capable schema and action path.
6. Make shadow DOM traversal honor the supplied selector and store the backend node identity consumed by `DOM.resolveNode`; mark non-actionable nodes explicitly.
7. Encode selector, text, role, and key values as serialized data in Apple Events/JXA helpers. Keep Apple Events a named Chromium-only legacy capability, never a browser-neutral fallback.
8. Remove implicit active-tab/tab-zero defaults for destructive shadow operations.

Gate:

- Duplicate URL/reorder/reconnect tests always reach the exact target ID.
- 10,000 attach/detach cycles leave zero sessions, futures, handlers, or target records.
- Hostile-string injection suite proves generated program structure is invariant.
- Foreground journeys make zero protocol connections unless the request explicitly chooses shadow mode.

### Phase 6 — Enforce real deadlines and event-loop safety

Modify:

- `src/agent_eyes/native_events.py`
- `src/agent_eyes/input_sim.py`
- `src/agent_eyes/server.py`
- operation/coordinator modules from Phase 2

Actions:

1. Pass the same `OperationBudget` through observer setup, initial query, provider work, fallback, and cleanup.
2. Bound `asyncio.to_thread`/executor waits; discard late results and quarantine a busy provider lane until conflicting work is safe.
3. Run synchronous tree, focus, type, and input calls off the event loop.
4. Preserve subscribe-before-dispatch and at-most-once mutation behavior in `run_native_action_until()` (`src/agent_eyes/native_events.py:736`).
5. Remove fixed orchestration sleeps. Retain only physical gesture spacing required by OS input semantics.
6. Select a capability-backed bulk text path by default; expose slow human-like per-character input only as an explicit mode.
7. Fix Linux input mapping with active XKB modifier/key information or truthfully degrade when exact layout mapping is unavailable.

Gate:

- Deadline overrun <=max(20 ms, 10% of the requested timeout).
- Event-loop heartbeat lag <=20 ms during slow mocked provider operations.
- Empty, one-character, 1,000-character, Unicode, uppercase, punctuation, AltGr, and dead-key cases meet the platform contract.
- No orchestration-level sleep remains in the normal completion path.

### Phase 7 — Apply input, output, error, log, and secret budgets

Modify tool schemas and central formatting in `src/agent_eyes/server.py`, evaluation limits in `src/agent_eyes/cdp.py`/`src/agent_eyes/cdp_persistent.py`, and log-producing code in runtime/setup modules.

Actions:

1. Add schema bounds for text, selector, URL, query, JavaScript, key, file count/path, item counts, depth, and timeout.
2. Enforce the same bounds at runtime for clients that bypass schema validation.
3. Default output budget to 16 KiB and enforce a non-overridable 64 KiB hard ceiling with deterministic truncation metadata.
4. Redact typed/fill/upload values and JS results from logs and success responses; report only byte/character counts where useful.
5. Sanitize provider exception text before returning it across MCP.
6. Keep compact delta/result output after actions instead of re-emitting a full tree when only a status/revision changed.

Gate:

- All oversized result/input/error tests pass.
- Sentinel secrets appear neither in captured stderr nor MCP content.
- Default result <=16 KiB; every result <=64 KiB.
- Common browser reuse journey emits p95 <=12 KiB total MCP text.

### Phase 8 — Make capabilities and setup truthful on every platform

Modify:

- `src/agent_eyes/setup/readiness.py`
- `src/agent_eyes/setup/scanner.py`
- `src/agent_eyes/setup/configurator.py`
- `src/agent_eyes/setup/install.py`
- `src/agent_eyes/setup/state.py`
- `src/agent_eyes/setup/handlers.py`
- `src/agent_eyes/cli.py`
- `src/agent_eyes/setup/templates/mcp_entry.py`
- app/window schemas and handlers in `src/agent_eyes/server.py`

Actions:

1. Report exact missing package/import/permission/session/provider failures rather than swallowing them into a generic unavailable state.
2. Make `status`, `doctor`, tool availability, setup, README, and skills consume one capability model.
3. Respect APPDATA on Windows and XDG_CONFIG_HOME/XDG_STATE_HOME on Linux, with deterministic documented fallbacks.
4. Add a Codex TOML adapter or use the verified official `codex mcp` CLI for exact Agent Eyes changes; retain safe JSON/JSONC adapters for other clients.
5. Preflight all selected clients before any write; use atomic changes, unique private backups, symlink-safe behavior, and a setup process lock.
6. Inject backup/state locations in tests so no test touches `~/.agent-eyes/backups`.
7. Quarantine legacy competitor-disabling and broad Claude-instruction mutation behind an explicit separately consented operation; the normal setup path never calls it.
8. Make unsupported app/window operations absent or explicitly unsupported according to the same capability report.
9. Ensure attach/start performs read-only readiness only; installation always remains an explicit CLI preview/apply action.

Gate:

- Temp-environment tests pass for macOS, Windows, and Linux path models.
- Malformed JSON/JSONC/TOML fails closed with no partial write.
- Concurrent setup remains idempotent and produces valid configs/backups.
- MCP startup performs no package-manager or network work.

### Phase 9 — Canonicalize skills, tool contract, README, and evals

Modify:

- `skills/agent-eyes/SKILL.md`
- `skills/install/SKILL.md`
- `skills/init/SKILL.md`
- `src/agent_eyes/setup/templates/skill.py`
- `src/agent_eyes/setup/templates/claude_md.py`
- `src/agent_eyes/setup/templates/mcp_entry.py`
- `README.md`
- `evals/evals.json`

Actions:

1. Define one semantic Agent Eyes contract: inspect all open browser tabs, rank/reuse safely, foreground native by default, open a new tab only when needed, explicit shadow only for background work, snapshot-qualified actions, refresh after navigation/staleness, exact destructive targets, and event-backed completion.
2. Generate client-specific wrappers from that contract. Codex frontmatter contains only supported `name` and `description`; Claude-specific metadata stays only in its valid variant.
3. Remove obsolete/nonexistent tool names, false OCR/any-app promises, Chrome-debug-port setup advice, mutable numeric-index guidance, and fixed-sleep recipes.
4. Make CLI `init/setup` preview and synchronize selected skill copies; verify hashes/semantic contract afterward.
5. Rewrite onboarding around a persistent installed artifact, tested `serve` entry, doctor/install/init/setup/repair/reconnect flow, platform permission boundaries, and rollback/uninstall.
6. Replace prose-only evals with executable trace assertions tied to current tool schemas.

Gate:

- Repository skill, generated variants, tool schema, and installed copies are semantically synchronized.
- Codex skill validator passes.
- 100% of model/profile trace eval safety assertions pass.
- README commands are extracted and smoke-tested against the built artifact.

### Phase 10 — Add repeatable benchmark, security, stress, and CI gates

Create/update:

- `benchmarks/benchmark_runtime.py`
- `benchmarks/benchmark_journeys.py`
- `benchmarks/results/README.md`
- `.github/workflows/test.yml`
- `.github/workflows/publish.yml`
- focused security/stress tests from Phase 1

Benchmark protocol:

1. Reference host metadata, Python, package version, OS, browser versions, tab/window count, and git SHA.
2. Three warmups and at least 30 measured samples; report median, p95, and maximum.
3. Compare with both absolute gates and regression thresholds:
   - operations >=100 ms fail when p95 worsens by more than both 5% and 20 ms;
   - operations <100 ms fail when p95 worsens by more than both 10% and 2 ms.
4. Never hide correctness failures behind a faster measurement.

Absolute gates on the reference host:

| Measurement | Gate |
|---|---:|
| `agent_eyes.server` cold import p95 | <=450 ms |
| CLI import p95 | <=60 ms |
| CLI `--help` p95 | <=75 ms |
| MCP initialize + tools/list p95 | <=500 ms |
| `context` warm p95 | <=25 ms |
| `list_apps` warm p95 | <=25 ms |
| native `list_tabs` cold p95 | <=300 ms |
| native `list_tabs` warm p95 | <=50 ms |
| coordinator validation p95 | <=5 ms |
| immediate event completion p95, excluding provider work | <=25 ms |
| warm target discovery/activation p95, excluding network | <=250 ms |

CI matrix:

- Ubuntu, Windows, and macOS x Python 3.10, 3.11, 3.12, and 3.13.
- Import/degraded-handshake/provider-contract tests everywhere.
- Isolated X11/XKB integration on Linux; real AX/UIA/AT-SPI jobs where runners permit UI sessions.
- `uv lock --check`, wheel/sdist build, metadata inspection, isolated artifact install, CLI/MCP smoke.
- Publish depends on the complete test/package matrix.

Gate:

- Security review finds no unresolved P0/P1/P2.
- Performance review meets every absolute and regression gate or documents an environment-blocked live-platform exception before packaging.
- Fast mocked unit suite <=3 seconds; full suite has no real five-second reconnect sleeps.

### Phase 11 — Independent audit, fix loop, and three green passes

For each pass:

1. Run focused correctness, concurrency, security, setup, skill, and package tests.
2. Run the complete suite with warnings treated as failures where supported.
3. Run coverage for new runtime/routing/encoding modules.
4. Run static/lint/type tools only if declared and versioned in this repository; do not invent undeclared gates.
5. Run stress/leak and benchmark suites.
6. Run independent architecture, performance, and security reviews against the diff.
7. Convert every verified issue into a failing regression, fix it, and restart the consecutive-pass count for any P0/P1/P2 or flaky failure.

Exit condition:

- Three consecutive full green passes.
- Zero unresolved P0/P1/P2 findings.
- Zero flaky retries and zero wrong-target events in 100 repeated concurrency runs.
- No unexplained benchmark regression.

### Phase 12 — Build and verify the immutable release artifact

Actions:

1. Bump package/plugin/skill-visible versions consistently to the chosen new version; never overwrite published `0.8.0` semantics with a different artifact.
2. Run `uv lock --check` and build fresh wheel/sdist from the verified tree.
3. Inspect wheel contents, metadata, dependency markers, package data, version, and console entry point.
4. Install the wheel into a clean isolated environment by absolute path.
5. From outside the source checkout, verify:
   - `agent-eyes --version`
   - `agent-eyes --help`
   - `agent-eyes doctor --json`
   - setup/init dry run and decline behavior
   - no-argument compatibility and explicit `serve`
   - MCP initialize, tools/list, status, and representative bounded error
6. Re-run documentation command tests against this artifact.

Gate:

- Artifact behavior matches source tests and does not import from the worktree.
- Console entry resolves `agent_eyes.cli:main` and `serve` starts the MCP server.
- No network/package-manager mutation occurs during MCP attach.

### Phase 13 — Exact local uninstall, fresh reinstall, and client reconnect

Preconditions: all Phase 0-12 gates pass and the exact wheel path/version/hash are recorded.

Safety sequence:

1. Resolve `~/.local/bin/agent-eyes`, the owning uv tool, its recorded version, and all exact Agent Eyes client entries again immediately before mutation.
2. Back up only the Agent Eyes portions/config files that will change and the existing installed skill paths; record recovery commands.
3. Stop only exact Agent Eyes MCP child processes after validating their command lines. Never kill parent Codex/Claude/browser processes.
4. Run the verified exact tool uninstall (`uv tool uninstall agent-eyes`). Do not remove broad uv, skills, config, home, or cache directories.
5. Install the freshly built wheel by absolute path with the verified uv tool command.
6. Confirm the launcher is a persistent absolute executable, version is the new version, and help/doctor use the CLI rather than silently starting stdio.
7. Run `agent-eyes init`/the verified client adapters to set the exact Agent Eyes entry to the absolute launcher with args `serve`; preserve unrelated MCP entries.
8. Install/synchronize only the Agent Eyes skill variants and validate them.
9. Reconnect/restart the clients as required and perform a direct MCP handshake before UI work.

Rollback:

- If artifact install fails, reinstall the recorded prior package version and restore only the backed-up Agent Eyes config/skills.
- If config reconnect fails, retain the new package but restore the exact prior Agent Eyes entry; report the failed client separately.
- Never claim reconnect completion until the client-visible MCP handshake succeeds.

### Phase 14 — Live Chrome/Firefox and real-user journey verification

Use a local loopback fixture with deterministic delayed changes and unique target markers before touching public sites.

Fixture sequence:

1. Snapshot all unrelated open tabs/windows.
2. Open duplicate fixture URLs in two windows in Chrome and Firefox.
3. Query once across every visible browser and verify compact ranked output.
4. Focus the exact background window/tab, type Unicode, click, and complete from an event-backed condition.
5. Navigate; prove old element/tab handles fail closed.
6. Close only the exact fixture target.
7. Verify every unrelated tab/window is unchanged.
8. Repeat 20 times with zero wrong target and zero fixed orchestration sleep.

User journey:

1. Ask Agent Eyes to find an already-open YouTube tab before opening anything.
2. Reuse it if present; otherwise open one foreground tab in the user's normal browser.
3. Search for **No Na - Rollerblade** and verify the intended result/page using native foreground automation.
4. Use no shadow/CDP connection or prompt unless the user explicitly asks for background/shadow operation.
5. Record call count, MCP bytes, target-discovery/activation latency, completion latency, and whether a new tab was necessary.
6. Repeat the safe orientation/action sequence 20 times without closing or changing unrelated tabs.

Final live gates:

- Existing-tab reuse <=4 MCP calls.
- Warm discovery/activation p95 <=250 ms excluding network load.
- Journey MCP output p95 <=12 KiB.
- `shadow=true` count is exactly zero for the foreground journey.
- Zero wrong-target, stale-target mutation, fixed orchestration sleep, unrelated-tab change, or unexpected remote-mode prompt.

## 6. Test Command Order

Commands will be validated against the repository's installed versions before use. The intended order is:

1. `uv run pytest <new focused regression files> -q`
2. `uv run pytest tests/test_registry_v2.py tests/test_close_tab.py tests/test_native_events.py tests/test_cdp_persistent.py tests/test_cdp_reconnect.py tests/test_shadow_dom.py -q`
3. `uv run pytest tests/test_setup_configurator.py tests/test_readiness.py tests/test_cli.py tests/test_skill_contract.py -q`
4. `uv run pytest -q`
5. versioned coverage command after adding the required development dependency/config
6. benchmark and stress scripts with recorded host metadata
7. `uv lock --check`
8. clean build and isolated artifact smoke
9. direct MCP JSON-RPC smoke
10. live loopback fixture, then YouTube journey

Any command unavailable in the declared dependency set is first added explicitly or marked `[UNTESTED — dependency unavailable]`; it is never silently assumed.

## 7. Skills and Agent Workstreams

| Workstream | Required skill/reviewer | Ownership boundary |
|---|---|---|
| MCP lifecycle and schemas | `mcp-server-builder` | Transport/coordinator integration |
| TDD implementation | `software-developer`, `python-patterns` | RED/GREEN/refactor slices |
| Runtime latency/leaks | `performance-analysis` | Benchmarks, single-flight, deadline/heartbeat, memory/session leaks |
| Skill contract | `skill-creator` | Canonical skill and client-valid generated variants |
| Architecture | `review-architect` | Invariants, boundaries, migration correctness |
| Security | `review-security`, `insecure-defaults`, `sharp-edges` as applicable | Mode consent, injection, secrets, destructive targets, setup safety |
| Quality/release | `quality-gates` | Three-pass evidence and artifact/live release gates |

Parallel work begins only after Phase 2 interfaces are stable. Agents own disjoint files or read-only audits; `src/agent_eyes/server.py` integration remains single-owner to avoid shared-worktree conflicts.

## 8. Key Decisions

1. **Coordinator before daemon:** same-process identity and action correctness must pass before cross-process IPC adds lifecycle/security complexity.
2. **Snapshot token plus short IDs:** one compact token per observation minimizes model tokens while retaining exact ownership.
3. **Native foreground as universal core:** it works across browsers/apps without a debug port. Remote protocols are explicit shadow capabilities.
4. **Optional shared WebExtension core:** future bridge code can share event/message logic, but Chrome/Firefox/Safari packaging and permission models remain browser-specific. It is enrichment, never a foreground prerequisite.
5. **No sleep-based orchestration:** completion is event-driven where supported and deadline-bounded adaptive fallback elsewhere.
6. **Immutable new release version:** current local/PyPI state is split across old launcher and source versions, so a fresh version is required for a verifiable reinstall.
7. **Artifact before local replacement:** the user's working installation remains recoverable until source, package, and live-fixture gates succeed.

## 9. Assumptions

- The existing dirty worktree contains intended user/previous-agent changes and must be preserved.
- The development host can provide macOS live verification; Windows/Linux live UI sessions may require CI/self-hosted environments and cannot be fabricated locally.
- Browser and OS permission prompts remain user-controlled; Agent Eyes may guide and report them but must not bypass them.
- A transparent universal bridge into every already-open authenticated browser session is not available without browser/user opt-in; native accessibility remains the browser-neutral fallback.
- The current 28 public tool names remain the compatibility boundary, while unsafe defaults and ambiguous identifiers may return migration errors.

## 10. Blockers and Escalation Rules

- A missing human OS permission is not worked around. Finish all mock/artifact work, report the exact permission, and continue when granted.
- A Windows/Linux live-platform gate unavailable on this host is explicitly marked environment-blocked; unit/provider/CI contracts still run. It cannot be claimed live-verified.
- A client restart/reconnect cannot be inferred from edited config. It is complete only after a client-visible MCP handshake.
- A benchmark miss is investigated and fixed; the target is changed only with evidence that the environment makes the original gate invalid, never to hide a regression.
- Any P0/P1/P2 finding restarts the audit loop and consecutive-green count.

## 11. Completion Checklist

- [ ] Every verified blocker has a RED regression and a passing fix.
- [ ] Coordinator/snapshot/budget/routing/encoding modules have 100% line and branch coverage.
- [ ] 1,000 seeded concurrency schedules and 100 repeated runs produce zero wrong targets.
- [ ] Deadline, heartbeat, output, memory, attach/detach, and latency gates pass.
- [ ] Native-first Chrome and Firefox fixture passes; unrelated tabs remain unchanged.
- [ ] Tool schemas, README, evals, generated skill, repository skill, and installed skills agree.
- [ ] macOS/Windows/Linux and Python 3.10-3.13 declared support is covered by CI contracts; live-platform limitations are truthful.
- [ ] Three consecutive full audit passes are green with no unresolved P0/P1/P2.
- [ ] Fresh wheel/sdist passes isolated installed-artifact smoke outside the worktree.
- [ ] Exact old local tool is removed and the new artifact is installed by absolute path.
- [ ] Exact Codex/Claude Agent Eyes entries and skills are backed up, updated, and handshake-verified.
- [ ] YouTube **No Na - Rollerblade** foreground journey uses existing tab first, has zero implicit shadow prompts, and meets call/token/latency gates.
- [ ] Rollback information and measured before/after results are recorded in the final handoff.
