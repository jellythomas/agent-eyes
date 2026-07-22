# Agent Eyes Transaction Performance: Implementation Plan

**Date:** 2026-07-22

**Approved design:** `docs/plans/2026-07-22-agent-eyes-transaction-performance-design.md`

**Status:** Approved for autonomous TDD implementation

**Target release:** `0.10.0`

**Branch:** `codex/docs-agent-eyes-reference`

**Delivery rule:** preserve every existing public tool, add the fast path, and release only from the exact wheel that passes the deterministic and live gates below.

## 1. Summary

Agent Eyes will add two universal MCP tools: `observe_target` for one compact,
target-scoped discovery call and `execute` for one bounded transaction. Known tasks use
one `execute` call. Exploratory tasks use one `observe_target` call followed by one
`execute` call. Existing primitives stay callable for compatibility and bounded
recovery.

The implementation is deliberately layered instead of adding more orchestration to the
server module. Public schemas and dispatch are currently centralized in
`src/agent_eyes/server.py:248-1230` and `src/agent_eyes/server.py:1262-1384`. The new
pure contract, locator, transaction, target-resolution, and telemetry modules will be
injected into thin server handlers. No implementation of `observe_target`, an Agent
Eyes transaction runner, or transaction telemetry currently exists; an exact-symbol
search found only the approved design document.

The fast path stays native-first and browser-neutral. It does not require Chrome remote
debugging, Playwright, an extension, or a background broker. Explicit shadow execution
continues to require an exact shadow target. A broker is reconsidered only if the
completed in-process implementation misses the reference-Mac p95 gate.

## 2. Scope Verification

| User requirement | Planned delivery | Release proof |
|---|---|---|
| Reuse an already-open tab first | Resolve all visible native browser targets once, reject tied best matches, activate the exact chosen target, and open only with explicit `url` plus `on_missing=open`. Current inventory already collects every visible supported browser process at `src/agent_eyes/browser_inventory.py:226-319`, while current best-match selection does not reject a top-score tie at `src/agent_eyes/browser_inventory.py:412-432`. | Known and discovery fixture counters show one inventory, one activation, and zero unnecessary new tabs. |
| Support browsers other than Chrome | Keep native accessibility as the foreground provider and never probe shadow mode implicitly. Browser targets already carry provider-owned element and window references at `src/agent_eyes/browser_inventory.py:154-179`. | Chrome, Firefox, Safari, and Edge capability evidence where available; zero shadow probes in every foreground fixture. |
| Preserve whole-computer access | Accept exact native `pid` targets for desktop applications as well as browser `target_id` or browser query targets. The adapter protocol already exposes app listing, PID trees, exact subtree refresh, actions, and focus operations at `src/agent_eyes/adapters/base.py:136-179`. | Cross-platform fake-adapter tests plus live macOS browser and desktop-app transactions. |
| Stop fixed orchestration sleeps | Register the native/provider condition before dispatch and use the existing bounded event/adaptive fallback primitive, whose action is never retried at `src/agent_eyes/native_events.py:880-905`. | AST fixed-sleep gate remains zero outside the existing physical-input, adaptive-fallback, and setup-readiness allowlist. |
| Make UI work feel immediate | One MCP transaction, one lock acquisition, one target resolution, one activation, at most one initial full observation, in-memory locators, scoped refresh, and compact output. | Agent Eyes median at most 1 second and p95 at most 3 seconds on 30 warm reference-Mac runs. |
| Reduce model reasoning and tokens | Move the deterministic interaction sequence into the transaction runner; cap `execute` output at 2 KiB and `observe_target` output at 4 KiB. The formatter already supports a caller-specific byte limit at `src/agent_eyes/result_format.py:167-216`. | Known calls = 1, discovery calls at most 2, no intermediate full tree, and exact byte assertions. |
| Remain safe for inline PR comments and other writes | Require at most one explicit `external_write`, make it the last action, execute every mutation once, stop on `OUTCOME_UNKNOWN`, and never retry an uncertain submit. | Consequential mutation counter is exactly one on success and never greater than one in failure, cancellation, and stress schedules. |
| Keep setup simple and synchronize the skill | Reuse the existing setup transaction, add the tools to its allowlist, and update the canonical/generated/installed skill to prefer `execute`, then `observe_target`. Setup already writes MCP and skill artifacts together at `src/agent_eyes/cli.py:475-550`. | Exact-wheel `setup`, restarted clients, installed skill equality, MCP handshake, version, and readiness checks. |
| Keep legacy users working | Add tools and handlers without removing or changing existing public tool names or accepted legacy inputs. The existing O(1) dispatch table is at `src/agent_eyes/server.py:1336-1384`. | Full regression suite and schema snapshots for legacy tools. |

## 3. Verified Starting Architecture

1. `call_tool` validates the public schema, checks runtime capabilities, dispatches, and
   applies one global formatter at `src/agent_eyes/server.py:1262-1333`. It has no
   transaction timing envelope or tool-specific byte limit.
2. A native tree call can load at the requested depth and then repeat at depth 20 when
   web content is sparse at `src/agent_eyes/server.py:1592-1646`.
3. `find` with a PID loads from the provider, whereas snapshot-backed search filters
   stored elements in memory at `src/agent_eyes/server.py:1844-1924`. The fast path must
   never repeat PID-based `find` calls.
4. Completed browser inventories are single-flight only while concurrent callers share
   the producer; completed flights are removed at `src/agent_eyes/coordinator.py:55-86`
   and `src/agent_eyes/coordinator.py:210-227`. Sequential reuse therefore needs an
   explicit short-lived cache.
5. `ObservationSnapshot` already binds provider, mode, target, generation, revision,
   and immutable element records at `src/agent_eyes/observations.py:15-35`; the store is
   bounded to 32 snapshots, 500 elements, and a 60-second memory TTL by default at
   `src/agent_eyes/observations.py:45-82`.
6. `OperationBudget` is one absolute monotonic deadline at
   `src/agent_eyes/operation.py:55-127`, but `run_native_action_until` uses the supplied
   parent budget instead of composing its local timeout at
   `src/agent_eyes/native_events.py:880-905`. A child-budget primitive is required.
7. `wait_for_native_element` calls `adapter.find_elements` during every condition check
   at `src/agent_eyes/native_events.py:1055-1092`; this is unsuitable for transaction
   confirmation because platform `find_elements` may rebuild a PID tree.
8. Successful native click, scroll, and hover returns do not consistently invalidate
   the observed target at `src/agent_eyes/server.py:2364-2429`,
   `src/agent_eyes/server.py:5265-5329`, and `src/agent_eyes/server.py:5553-5652`.
9. Public click already has a resolved internal handler, and fill-form already performs
   several resolved type operations under one coordinator lock at
   `src/agent_eyes/server.py:2114-2155` and `src/agent_eyes/server.py:5462-5548`. These
   are useful integration patterns, but their string results and multi-strategy action
   fallbacks are not the structured one-dispatch transaction kernel.
10. The runtime input validator supports the bounded object/array/type subset plus
    `if`/`then`/`else`, `allOf`, `anyOf`, and `not` at
    `src/agent_eyes/input_validation.py:17-170`. Cross-step aliases, consequence order,
    and retry safety require a separate semantic validator.
11. The full catalog must remain at most 16 KiB at
    `tests/test_token_budget.py:10-21`. The recorded v0.9.0 catalog is 15,143 bytes, and
    a locally executed compact two-tool schema prototype was 2,539 bytes before real
    descriptions; catalog compaction is therefore release-critical.
12. The installed-artifact smoke currently expects 28 tools on macOS and 25 elsewhere
    at `scripts/smoke_installed_artifact.py:20-21`. Two universal tools make those
    expectations 30 and 27.

## 4. Frozen Public Contract

The first implementation freezes the following additive surface before any provider
work is written.

### 4.1 `observe_target`

- Target selector: exactly one of provider-qualified `target_id`, native `pid`, or
  browser `query`.
- `intent`: `inspect` (do not change focus) or `interact` (activate and verify once).
- `selectors`: zero to eight locator objects evaluated together from one observation.
- Locator fields: `role`, `name`, `value`, and `match` (`exact`, `contains`, `prefix`,
  or `suffix`). At least one filter field is required per locator.
- `max_results`: 1-20. `deadline_ms`: 1-30,000.
- Normal output: target identity, compact snapshot token/revision, bounded matches,
  scan/cache metadata, and no full unchanged tree; at most 4 KiB.

### 4.2 `execute`

- `target.mode`: `foreground` by default; `shadow` is explicit.
- Foreground target: exactly one of `target_id`, `pid`, or browser `query`. A valid
  matching snapshot may be supplied as an optimization, never as proof that external
  state is unchanged.
- Shadow target: exact provider-qualified `target_id` only; no query, PID, implicit
  fallback, or foreground hover substitution.
- Missing browser target: default `fail`; `open` is accepted only with an explicit
  bounded `http`, `https`, `file`, or `about` URL.
- One to eight steps using only `locate`, `hover`, `click`, `type`, `press_key`,
  `scroll`, and `expect`.
- Local aliases are unique, defined before use, and revision-scoped. Every locator
  resolves exactly one element.
- At most one step declares `consequence=external_write`; it must be the last mutating
  step. No loops, JavaScript, recursion, nested transactions, or hidden branching.
- The top-level final `expect` is optional but required by the skill for consequential
  workflows unless the provider can return an equivalent verified postcondition.
- `deadline_ms`: 1-30,000; the installed skill defaults interactive fast-path examples
  to 3,000 ms.
- Normal output: stable status/code, target, completed-step count, final expectation,
  elapsed milliseconds, retry safety, and optional compact failure snapshot; at most
  2 KiB.

The public JSON schemas stay shallow: one common bounded step object with an `op` enum
and a bounded superset of fields. Operation-specific shapes and cross-step invariants
are enforced by the pure semantic validator. This fits the validator implemented at
`src/agent_eyes/input_validation.py:17-170` and avoids unsupported `$ref`, `$defs`,
`oneOf`, or recursive schema expansion.

## 5. Ordered Implementation Plan

### Phase 0 - Preserve baseline evidence

1. Record clean/dirty paths, current HEAD, Python and uv versions, resolved local
   executable, installed Agent Eyes version, MCP tool count, readiness, catalog bytes,
   and existing v0.9.0 benchmark artifact hashes.
2. Run the complete current suite once before RED tests; distinguish pre-existing
   failures from task-owned failures.
3. Preserve all v0.9.0 JSON and release documents as immutable history. Add v0.10.0
   artifacts rather than rewriting old evidence.

Gate: reproducible baseline, no unrelated worktree changes, and no local uninstall or
client restart.

### Phase 1 - Contract and semantic validation (RED -> GREEN)

Create:

- `src/agent_eyes/transaction_contract.py`
- `tests/test_transaction_contract.py`

Modify:

- `src/agent_eyes/tool_contract.py`
- `src/agent_eyes/input_validation.py` only if a missing primitive is proven necessary
- `tests/test_tool_contract.py`
- `tests/test_input_validation.py`
- `tests/test_token_budget.py`

Steps:

1. Add failing schema tests for both tool names, bounded target/selector/step fields,
   foreground defaults, explicit shadow identity, and unchanged legacy schemas.
2. Add failing pure semantic tests for target exclusivity, maximum eight steps and
   selectors, supported operations, unique/ordered aliases, unique locators, open URL
   requirements, one provider, one final consequence, and forbidden branches.
3. Add stable errors `INVALID_TRANSACTION`, `AMBIGUOUS_ELEMENT`, and
   `OUTCOME_UNKNOWN`, with an explicit retry-safety table, to the existing stable error
   enum at `src/agent_eyes/operation.py:28-52`.
4. Implement frozen transaction dataclasses and a pure `parse_transaction` validator.
   It must not import adapters, workers, server globals, or platform frameworks.
5. Add the two compact tool schemas. Compact existing descriptions and repeated schema
   prose while preserving all native-first and explicit-shadow cues asserted at
   `tests/test_token_budget.py:24-55`; do not raise the 16 KiB gate.

Gate: contract tests pass, malformed input reaches no readiness/provider spy, every
schema is Draft 2020-12 valid, and the complete catalog remains at most 16 KiB.

### Phase 2 - Composable deadlines and revision invalidation

Modify:

- `src/agent_eyes/operation.py`
- `src/agent_eyes/server.py`
- `tests/test_operation_budget.py`
- `tests/test_server_snapshots.py`

Steps:

1. Add RED tests for `parent.child(max_seconds)` returning a deadline equal to
   `min(parent.deadline, now + max_seconds)`, sharing the same monotonic clock, and
   rejecting invalid limits.
2. Implement the child budget and pass bounded children to exact focus, target
   activation, observer startup, individual action dispatch, and condition refresh.
3. Add RED tests proving focus/activation cannot consume the full transaction deadline.
4. Make every successful or uncertain native mutation invalidate the global snapshot
   and short inventory cache exactly once. Keep the transaction's local reference table
   independent so it can perform its explicit scoped refresh.

Gate: no child exceeds its local cap or parent remainder; success and uncertainty both
advance/invalidate state; legacy handlers retain their public result strings.

### Phase 3 - Shared locators and exact target resolver

Create:

- `src/agent_eyes/locators.py`
- `src/agent_eyes/target_resolver.py`
- `tests/test_locators.py`
- `tests/test_target_resolver.py`

Modify:

- `src/agent_eyes/browser_inventory.py`
- `tests/test_browser_inventory.py`

Steps:

1. Extract role/name/value match logic into a pure locator engine supporting exact,
   contains, prefix, and suffix modes, optional transaction-local `within` scope, a
   parent index, deterministic ordering, and ambiguity errors.
2. Reuse the locator engine from legacy `find` to prevent semantic drift.
3. Introduce provider-neutral resolved targets for native PID apps, native browser
   windows/tabs, and exact shadow targets. Browser querying reuses current ranking but
   rejects equal top candidates rather than silently taking the first.
4. Add a 300 ms monotonic completed-inventory cache inside the resolver. Key it by
   adapter/provider/mode and invalidate on app, window, tab, navigation, focus, and any
   transaction mutation. Failed/cancelled producers are never cached; concurrent misses
   continue using coordinator single-flight.
5. Validate live provider-owned window/tab references immediately before activation.

Gate: exact and query resolution, tied-match failure, TTL expiry, every invalidation
event, cancellation, adapter separation, and concurrent miss behavior pass without
sleep-based tests.

### Phase 4 - Compact target observation

Create:

- `src/agent_eyes/target_observation.py`
- `tests/test_observe_target.py`

Modify:

- `src/agent_eyes/server.py`

Steps:

1. Factor native tree loading from the rendering handler at
   `src/agent_eyes/server.py:1592-1703` into an injected observation service.
2. For browser targets, refresh the selected live window reference through the adapter
   `get_subtree` contract at `src/agent_eyes/adapters/base.py:151-159`; for exact native
   PID apps, load one PID tree.
3. Build one immutable snapshot, local element index, and parent index. Evaluate every
   selector in memory and render only target metadata and matches.
4. `intent=inspect` never activates. `intent=interact` activates and verifies the exact
   resolved target once.
5. A valid supplied snapshot avoids a provider scan; a stale snapshot allows only one
   bounded scoped refresh.
6. Wire `_handle_observe_target` into readiness and dispatch.

Gate: all selectors share one observation; at most one full scan; no PID find loop;
ambiguous target/selector fails closed; result is at most 4 KiB.

### Phase 5 - Structured one-dispatch action kernels

Create:

- `src/agent_eyes/action_kernel.py`
- `tests/test_action_kernel.py`

Modify:

- `src/agent_eyes/server.py`

Steps:

1. Define a frozen `ActionOutcome` with stable status, dispatched flag, changed flag,
   retry safety, provider code, and no user text.
2. Extract unlocked resolved kernels for hover, click, type, press-key, and scroll.
   Public legacy wrappers continue acquiring their current locks and rendering their
   existing strings.
3. Preflight capability and focus before dispatch. Select one provider action. Do not
   iterate `press`, `click`, `confirm`, and `open` after dispatch; the current native
   click loop is at `src/agent_eyes/server.py:2364-2390` and must not be called by a
   transaction.
4. Preserve the existing may-have-run poisoning boundary at
   `src/agent_eyes/server.py:58-88`. Once a provider may have received a mutation,
   timeout/cancellation becomes `OUTCOME_UNKNOWN` and cannot be retried.
5. Register the event/condition before dispatch, reuse
   `run_native_action_until`, and inject a scoped locator refresh condition instead of
   `wait_for_native_element`.

Gate: every action dispatch counter is zero or one; event registration precedes it;
uncertainty stops; typed text never appears in the outcome, result, or logs.

### Phase 6 - Transaction state machine and MCP integration

Create:

- `src/agent_eyes/transactions.py`
- `tests/test_transaction_engine.py`
- `tests/test_transaction_integration.py`

Modify:

- `src/agent_eyes/server.py`
- `src/agent_eyes/result_format.py` only if a named budget helper improves reuse

Steps:

1. Implement a pure async state machine with injected ports for resolve, activate,
   observe, locate, action, refresh, and telemetry. It must not import `server.py`.
2. Validate the complete transaction before acquiring the coordinator or checking
   readiness.
3. Acquire one foreground coordinator slot for the complete transaction. Resolve one
   target, activate at most once, capture at most one initial full observation, and keep
   local aliases revision-scoped.
4. Execute steps linearly. `locate` is in-memory; actions dispatch once; event-backed
   expectations trigger only scoped refresh; any error or uncertainty stops later
   steps.
5. Add `_handle_execute` to the dispatch table and use existing internal server
   capabilities through injected adapters/workers rather than calling public handlers
   while the coordinator lock is held.
6. Add per-tool result limits in `call_tool`: 4 KiB for `observe_target`, 2 KiB for
   `execute`, and the unchanged global default for legacy tools.

Gate: the deterministic Bitbucket-like known path is one public MCP call; discovery is
two; one target/inventory/activation/full initial scan; no shadow probe; one final
external write; bounded result; verified posted state.

### Phase 7 - Structured redacted telemetry

Create:

- `src/agent_eyes/telemetry.py`
- `tests/test_transaction_telemetry.py`

Modify:

- `src/agent_eyes/server.py`

Steps:

1. Record only allowlisted numeric/enumerated fields: tool, stable result code, total,
   queue, resolution, activation, observation, action, and wait durations; scans/nodes;
   cache state; completed steps; output bytes; and truncation.
2. Never record query, URL, title, selector, alias, label, typed text, page content,
   file path, raw arguments, raw result, or exception message.
3. Make the sink injectable and failure-isolated. Default operation has near-zero
   overhead and writes no stdout; MCP stdout remains protocol-only.
4. Add sentinel privacy tests across validation, readiness, success, ambiguity,
   deadline, cancellation, truncation, and unexpected exception paths.

Gate: exactly one trace per call, zero sentinel leakage in traces/logs/results, and a
broken sink cannot change the tool outcome.

### Phase 8 - Skill, setup, and documentation synchronization

Modify:

- `skills/agent-eyes/SKILL.md`
- `skills/agent-eyes/agents/openai.yaml`
- `src/agent_eyes/setup/templates/skill.py`
- `src/agent_eyes/setup/templates/openai_skill.py`
- `src/agent_eyes/setup/templates/claude_md.py`
- `src/agent_eyes/setup/templates/mcp_entry.py`
- `README.md`
- `docs/api/mcp-tools.md`
- `docs/api/agent-eyes-cli.md`
- `tests/test_setup_configurator.py`
- `tests/test_documentation_contract.py`
- `tests/test_readme_commands.py`

Steps:

1. Change the preferred workflow from the current granular sequence at
   `skills/agent-eyes/SKILL.md:16-32` to known=`execute`, discovery=`observe_target` then
   `execute`, primitives only for bounded recovery.
2. Keep the repository skill byte-for-byte synchronized with the generated template;
   that equality is already tested at `tests/test_setup_configurator.py:31-45`.
3. Add both tools to the static setup allowlist at
   `src/agent_eyes/setup/templates/mcp_entry.py:38-87` and update exact tool-name tests.
4. Document every property, result, error, side effect, safety rule, and schema-valid
   example. Update first-use, efficiency, complete tool list/counts, benchmark protocol,
   version, upgrade, and restart guidance.
5. Explain that `uvx agent-eyes@latest setup` performs install/config/skill
   synchronization; separate `install` and `init` are repair/advanced flows, not
   mandatory follow-up steps.

Gate: generated/repository skill equality, metadata equality, documentation coverage,
all examples schema-valid, all links resolve, and no README instruction asks for
implicit remote-browser mode.

### Phase 9 - Deterministic benchmarks and stress

Create:

- `benchmarks/transaction_fixture.py`
- `benchmarks/benchmark_transactions.py`
- `benchmarks/benchmark_live_transactions.py`
- `benchmarks/analyze_reference_agent_trace.py`
- `benchmarks/stress_transactions.py`
- `tests/test_benchmark_transactions.py`
- `tests/test_stress_transactions.py`

Modify:

- `benchmarks/benchmark_runtime.py`
- `.github/workflows/test.yml`

Fixture states: exact browser/window, virtualized diff rows, scroll reveal, hover-reveal
comment control, asynchronous editor insertion, typing, final save, and posted-state
verification. It exposes counters for calls, scans, activations, full/scoped
observations, event registration, dispatches, external writes, shadow probes, revisions,
and retained resources.

Deterministic CI gates:

- Known calls = 1; discovery calls at most 2.
- Full scans before first mutation at most 1.
- Activations at most 1.
- External writes exactly 1 on success and never greater than 1.
- Shadow probes = 0 for foreground journeys.
- `execute` at most 2 KiB; `observe_target` at most 4 KiB; catalog at most 16 KiB.
- Fixed orchestration sleeps = 0.
- Cancellation at every boundary and 32 queued transactions produce no overlap or
  post-deadline mutation.
- 10,000 repetitions retain no snapshots, refs, workers, subscriptions, locks, or
  cache entries and grow RSS by no more than max(10 MiB, 5%).

Hosted CI gates counts, safety, resource cleanup, and output budgets, not absolute wall
time. The current runtime benchmark explicitly separates live browser/network latency
at `benchmarks/benchmark_runtime.py:426-434`.

Reference evidence:

- Three warmups plus 30 real native-accessibility runs from the exact wheel.
- Agent Eyes controlled runtime median at most 1 second and p95 at most 3 seconds.
- Record OS, Python, browser, package version, wheel SHA-256, and provider phases.
- Report model/client time separately. A preloaded reference-agent median at most 3
  seconds is a stretch target, not a blended runtime release gate.
- Live Bitbucket is opt-in and uses only an explicitly authorized disposable PR.

### Phase 10 - Package, install, and release v0.10.0

Create:

- `docs/release/v0.10.0.md`
- `docs/release/v0.10.0-preflight.md`
- versioned v0.10.0 benchmark JSON files under `benchmarks/results/`

Modify:

- `src/agent_eyes/__init__.py`
- `.claude-plugin/plugin.json`
- `scripts/smoke_installed_artifact.py`
- `tests/test_smoke_installed_artifact.py`
- `tests/test_normalize_sdist.py`
- `.github/workflows/publish.yml` only if exact-wheel transaction proof is not already
  inherited from the reusable test workflow

Steps:

1. Bump runtime and plugin versions to 0.10.0 and test their equality.
   `pyproject.toml` already derives the package version from
   `src/agent_eyes/__init__.py` at `pyproject.toml:63-64`.
2. Update universal tool counts to 30/27 and make isolated-wheel smoke assert both new
   schemas, tool-specific output budgets, fast-path skill template, setup dry-run,
   version, readiness, and MCP handshake. Current smoke validates the catalog and status
   at `scripts/smoke_installed_artifact.py:59-103`.
3. Run lint, compile, Bandit, dependency audit, full pytest, contract, deterministic
   benchmark, stress, build reproducibility, Twine, artifact-content, and exact-wheel
   smoke gates on the declared CI matrix at `.github/workflows/test.yml:17-110`.
4. Run the reference-Mac live benchmark and attach its wheel hash to preflight evidence.
5. Only after all source and artifact gates pass: remove the exact old local uv-tool
   installation, install the exact newly built wheel, run `agent-eyes setup` for selected
   clients, restart changed clients, and repeat installed-artifact/runtime/live checks.
6. Merge to `main`, tag `v0.10.0`, create the GitHub release, and let trusted publishing
   run. Publishing already requires reusable full gates and verifies the tag matches the
   package version at `.github/workflows/publish.yml:15-43`.

Gate: source tree, built wheel, installed command, MCP server, generated skills, live
runtime evidence, tag, GitHub release, and PyPI artifact all identify the same 0.10.0
content and SHA-256 provenance.

## 6. Skills and Agents

- `brainstorming`: completed the architecture, safety, cache, benchmark, and rollout
  decisions before implementation.
- `implementation-planner`: produced this evidence-backed phased plan. The Jira-bound
  `planning-task` workflow was not used because this public repository has no Jira key;
  none was fabricated.
- `mcp-server-builder`: apply during the public schema/handler implementation.
- `python-patterns` and `software-developer`: apply during TDD implementation and module
  design.
- `performance-analysis`: apply to benchmarks, profiling, cache validity, and release
  gates.
- `agent-eyes`: update and validate the canonical computer-use workflow and installed
  skill.
- `quality-gates`: invoke before commits; because its generic runner does not support
  Python, use the checked-in Python lint/security/test/build/artifact gates as the
  authoritative fallback.
- Parallel read-only audits covered transaction architecture, test/benchmark evidence,
  and setup/docs/release consistency. Implementation edits remain owned by the primary
  agent to keep TDD slices coherent.

## 7. Quality Checklist

- [ ] Baseline and exact symbol/dependency searches saved.
- [ ] Every new behavior begins with a failing focused test.
- [ ] Every schema accepts documented examples and rejects malformed/ambiguous shapes.
- [ ] Semantic validation happens before readiness, locks, inventory, focus, or input.
- [ ] One foreground lock spans one transaction without nested-lock deadlock.
- [ ] Child deadlines never exceed local caps or parent remainder.
- [ ] Target ambiguity and locator ambiguity fail closed.
- [ ] Browser tasks inventory all visible supported browsers once before opening a tab.
- [ ] Desktop PID targeting remains provider-neutral.
- [ ] Foreground transactions never probe or fall back to shadow.
- [ ] Every mutation dispatches at most once; uncertainty stops all later steps.
- [ ] Successful and uncertain mutations invalidate revision/cache state.
- [ ] No fixed orchestration sleep is introduced.
- [ ] Typed/page/path/URL/query data is absent from telemetry, logs, and compact results.
- [ ] `execute` <=2 KiB; `observe_target` <=4 KiB; catalog <=16 KiB.
- [ ] Known calls =1; discovery calls <=2; full scans before mutation <=1.
- [ ] Median <=1 second and p95 <=3 seconds on 30 exact-wheel reference-Mac runs.
- [ ] Full Linux/macOS/Windows and Python 3.10-3.14 matrix passes.
- [ ] Stress, dependency, static-security, reproducible-build, and exact-wheel gates pass.
- [ ] README, MCP reference, generated skill, checked-in skill, and installed copies agree.
- [ ] No unrelated user files or historical v0.9.0 evidence are modified.

## 8. Key Decisions

1. Add `observe_target` and `execute`; preserve every legacy public tool.
2. Use a bounded transaction, not more model-driven primitive loops.
3. Release gates are Agent Eyes median at most 1 second and p95 at most 3 seconds;
   full preloaded reference-agent median at most 3 seconds remains a stretch target.
4. Use native accessibility as the universal foreground provider. Browser bridges are
   optional accelerators, never required or browser-specific core dependencies.
5. Keep one in-process coordinator for v0.10.0. Add a broker only if measured cold-start
   or cross-client overhead still fails p95 after the fast path.
6. Use a 300 ms completed inventory cache, no implicit cross-request tree cache, and
   explicit mutation invalidation.
7. Keep public schemas compact and enforce cross-step invariants in a pure semantic
   validator.
8. Keep the 16 KiB catalog budget; compact the existing catalog instead of increasing
   model context.
9. No new mandatory third-party package is planned. Existing adapter, worker, event,
   schema, and formatter interfaces cover the required implementation surface.
10. v0.10.0 is not released until the exact built and locally installed wheel passes the
    same deterministic contract, stress, and performance checks as the source tree.

## 9. Assumptions

- Existing primitive tools remain the fallback for workflows outside the initial seven
  transaction operations.
- Browser `query` resolution applies to visible native browser tabs/windows; arbitrary
  desktop applications use exact PID until a cross-platform stable app/window identity
  is proven.
- `on_missing=open` is a single pre-step navigation mutation and is allowed only in
  foreground browser mode with an explicit safe URL.
- A 60-second stored snapshot is a memory bound, not proof of live UI freshness, as
  shown by the store defaults at `src/agent_eyes/observations.py:45-82`.
- Platform-specific live browser coverage is capability-aware; deterministic fake
  adapters remain release-blocking on all CI operating systems.
- The user authorizes local source edits, tests, packaging, exact local reinstall, and
  client configuration updates in this repository. GitHub tag/release and PyPI publish
  occur only after every preflight gate is green.

## 10. Blockers and Concerns

1. **Catalog capacity:** only 1,241 bytes remain in the recorded v0.9.0 catalog. The
   compact schema POC proves the contract fits the current validator, not the full
   catalog. At least roughly 1.3 KiB plus real descriptions must be reclaimed.
2. **Full-agent three-second target:** model/provider scheduling is outside Agent Eyes'
   controlled runtime. It is reported separately and cannot block v0.10.0 unless the
   user later promotes it from stretch to release gate.
3. **Exact desktop window identity:** PID targeting is universal, but a stable
   cross-platform opaque window ID is not yet present in `UIElement`, whose identity is
   the provider-owned `platform_ref` at `src/agent_eyes/adapters/base.py:36-53`. The
   transaction retains that live reference only within one bounded call.
4. **Scoped refresh lineage:** `UIElement` stores children but not parents at
   `src/agent_eyes/adapters/base.py:36-53`. The transaction must build a local parent
   index; if the scoped live reference is invalid, it may perform one exact-window
   refresh, not a PID search loop.
5. **Action refactor risk:** legacy click/type paths contain multiple fallbacks. Thin
   wrappers and regression tests must preserve old contracts while the transaction uses
   one-dispatch kernels.
6. **Stale installed clients:** MCP clients cache tool catalogs and skills. The final
   setup step is insufficient until every changed client is restarted and the new MCP
   handshake is observed.
7. **No absolute hosted-CI latency gate:** shared hosted runners are too noisy for a
   credible one-second median. Deterministic work-count gates run in CI; absolute timing
   is release-blocking on the recorded reference Mac and exact wheel.

No blocker currently prevents Phase 1. Any failure to fit the two tools under 16 KiB,
retain one-dispatch safety, or meet exact-wheel p95 at most 3 seconds stops the release
and triggers a measured redesign rather than a relaxed gate.
