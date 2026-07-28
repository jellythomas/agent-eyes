# Agent Eyes Transaction Performance Design

**Date:** 2026-07-22

**Status:** Approved

**Target release:** v0.10.0

**Scope:** End-to-end model/tool latency, bounded foreground transactions, compact observations, and realistic performance gates

## 1. Outcome

Agent Eyes will add a provider-neutral transaction fast path without removing or
changing its existing MCP tools. A known task should complete in one MCP call. An
exploratory task should require one compact observation followed by one transaction.

The release has two distinct performance goals:

- Agent Eyes controlled runtime: median at most 1 second and p95 at most 3 seconds
  on the preloaded reference journey.
- Reference model/client journey: median at most 3 seconds as a stretch target on
  the same preloaded fixture.

Live model, website, rendering, and network latency is reported separately. It must
not be blended into the Agent Eyes runtime number.

## 2. Problem and verified evidence

The 2026-07-22 Bitbucket inline-comment attempt took 468 seconds end to end. It made
40 Agent Eyes calls, while Agent Eyes occupied about 40 seconds of unique wall time.
The workflow issued 15 `find`, 8 `tree`, and 6 `list_tabs` calls, but never reached
`type`, `fill_form`, or `press_key`.

The dominant runtime and protocol defects are verified in the current tree:

- PID-based `find` calls the native provider, while snapshot-based `find` filters
  already stored elements in process (`src/agent_eyes/server.py:1844-1893`). On
  macOS, the PID path rebuilds a depth-8 tree (`src/agent_eyes/adapters/macos.py:536-546`).
- `list_tabs` inventories and caches targets but does not activate them
  (`src/agent_eyes/server.py:3298-3346`). `tree` accepts a PID rather than a target ID
  (`src/agent_eyes/server.py:267-315`), and the macOS adapter reads the focused or
  first window (`src/agent_eyes/adapters/macos.py:435-459`).
- Current public actions and observations are separate dispatches
  (`src/agent_eyes/server.py:1336-1384`), forcing a model round trip between steps.
- The current journey benchmark measures only `list_tabs` followed by `new_tab`
  against mocked providers (`benchmarks/benchmark_journeys.py:66-156`). It cannot
  detect a real observation/search/action loop.
- The active v2 design already records the per-user broker, revision cache, and
  bounded deltas as remaining performance work
  (`docs/plans/2026-07-17-agent-eyes-v2-native-first-design.md:60-62`,
  `docs/plans/2026-07-17-agent-eyes-v2-native-first-design.md:207-223`).

The conclusion is that v0.9.0 optimized individual primitives but did not make the
end-to-end fast path structurally difficult to misuse.

## 3. Goals

1. Preserve every existing tool name and behavior.
2. Complete known tasks in one MCP call and exploratory tasks in at most two.
3. Resolve one target, activate once, and perform at most one full observation before
   the first mutation.
4. Keep all internal locate operations in memory unless an observed revision requires
   a scoped refresh.
5. Execute each mutation once and observe completion through events or bounded
   adaptive fallback.
6. Return only the final outcome or a compact, safe failure trace.
7. Remain browser-neutral and native-first; shadow mode stays explicit-only.
8. Measure provider time, model time, calls, scans, output, and reliability separately.

## 4. Non-goals

- Guaranteeing a three-second wall time for arbitrary external models, live websites,
  network conditions, or unloaded pages.
- Adding site-specific Bitbucket behavior to the universal runtime.
- Requiring Chrome remote debugging, a WebExtension, CDP, or a new browser package.
- Accepting arbitrary JavaScript, loops, recursion, or an open-ended workflow language.
- Retrying a mutation whose outcome is uncertain.
- Shipping the per-user broker before measurements prove it is required.

## 5. Alternatives considered

### 5.1 Tune existing primitives only

Improve descriptions, snapshot use, batched search, activation, and deadlines while
retaining the existing interaction sequence. This is low risk but still leaves the
model responsible for 12-20 serial decisions. It cannot meet the reference journey
target.

### 5.2 Add fused observation and single-action tools only

Combine inventory, activation, tree, and search into one observation; combine one
mutation, wait, and verification into one action. This reduces a typical journey to
4-8 calls and is safer than multi-step execution, but external model round trips still
make a three-second reference journey unlikely.

### 5.3 Add a bounded transaction executor

Add compact observation for discovery and a bounded transaction for execution. A
known task uses one call; an exploratory task uses two. Existing primitives remain
available. This has the largest test and safety burden, but it is the only approach
with a realistic path to the approved targets. This is the selected design.

## 6. High-level architecture

```text
MCP client
  -> observe_target (only when discovery is needed)
     -> target resolver
     -> optional exact activation
     -> one scoped observation
     -> compact snapshot and matches

MCP client
  -> execute
     -> target resolver
     -> exact activation
     -> transaction coordinator and total deadline
     -> scoped observation and local reference table
     -> bounded steps
        -> locate in memory
        -> mutate once
        -> event-backed condition
        -> scoped revision/refresh
     -> final expectation
     -> compact result and redacted telemetry
```

The transaction runs under one foreground coordinator lock. The current coordinator
already serializes foreground mutations and supports deadline-aware execution
(`src/agent_eyes/coordinator.py:88-105`, `src/agent_eyes/coordinator.py:165-191`).

Native accessibility remains the universal foreground plane. Optional browser bridges
may enrich metadata or revisions, but they are accelerators rather than dependencies.
Explicit shadow transactions require an exact shadow target and never act as fallback.

## 7. Public MCP contract

### 7.1 `observe_target`

Purpose: resolve one target and return only the state needed to plan a transaction.

```json
{
  "query": "Bitbucket PR title or URL",
  "intent": "interact",
  "selectors": [
    {"role": "button", "name": "comment", "match": "contains"}
  ],
  "max_results": 10
}
```

Rules:

- `intent=inspect` preserves focus.
- `intent=interact` activates the exact matched target.
- An exact provider-qualified `target_id` may replace `query`.
- Selectors are evaluated together from one observation.
- Target or selector ambiguity fails closed.
- Normal output is at most 4 KiB.

### 7.2 `execute`

Purpose: execute one bounded transaction without returning to the model between steps.

```json
{
  "target": {
    "target_id": "native:example",
    "snapshot": "nexample",
    "mode": "foreground"
  },
  "steps": [
    {"op": "locate", "as": "comment_button", "role": "button", "name": "comment"},
    {"op": "click", "ref": "comment_button", "expect": {"role": "textbox"}},
    {"op": "locate", "as": "editor", "role": "textbox"},
    {"op": "type", "ref": "editor", "text": "example"},
    {"op": "locate", "as": "submit", "role": "button", "name": "Save"},
    {"op": "click", "ref": "submit", "consequence": "external_write"}
  ],
  "expect": {"role": "article", "name": "posted comment condition"},
  "deadline_ms": 3000
}
```

The initial operation set is intentionally small:

- `locate`
- `hover`
- `click`
- `type`
- `press_key`
- `scroll`
- `expect`

Local names such as `comment_button` exist only inside the transaction. Locators use
exact, contains, prefix, or suffix matching. Every locator must resolve uniquely.

## 8. Transaction invariants

1. One target and one provider per transaction.
2. At most eight steps.
3. At most one consequential action, declared explicitly and placed last.
4. No loops, arbitrary JavaScript, recursion, or hidden branches.
5. Foreground is the default; shadow must be explicit at the transaction boundary.
6. Existing targets are ranked and reused first. Opening a tab requires an explicit
   URL and `on_missing=open`.
7. A mutation executes once. `OUTCOME_UNKNOWN` stops the transaction.
8. Zero or multiple locator matches stop the transaction.
9. Each child deadline is `min(local_limit, transaction_remaining)`.
10. Typed values and page data never appear in telemetry or result traces.

The existing schema hardener rejects unknown object properties and bounds arrays,
strings, integers, and timeouts (`src/agent_eyes/tool_contract.py:330-386`). The new
schemas use the same contract layer.

## 9. State and revision model

`ObservationSnapshot` already binds provider, mode, target, generation, revision, and
elements (`src/agent_eyes/observations.py:24-35`). The transaction adds a local
reference table and target revision tracker around that model.

Within a transaction:

1. Capture one scoped snapshot.
2. Resolve all locators from its stored elements.
3. Register the expected event before every mutation.
4. Advance or invalidate the affected target revision after a successful or uncertain
   mutation.
5. Refresh only the affected subtree when the event or validity check requires it.

Cross-request tree reuse is not implicit in the first release. The existing 60-second
snapshot TTL is a memory bound, not proof that external UI state is unchanged
(`src/agent_eyes/observations.py:45-82`). Cross-request reuse requires a live revision
source or a separately approved conservative policy.

A completed inventory may use a 250-500 ms cache. Tab, window, app, navigation, focus,
or transaction mutations invalidate it. Concurrent single-flight remains available,
but it is not treated as sequential caching (`src/agent_eyes/coordinator.py:55-86`,
`src/agent_eyes/coordinator.py:210-227`).

## 10. Completion and deadlines

Each mutation registers an accessibility or provider observer before dispatch. Success
is the requested condition, a revision that requires refresh, target disappearance,
provider failure, or the monotonic deadline. The current native event primitive already
registers before dispatch and avoids fixed completion delays
(`src/agent_eyes/native_events.py:880-900`).

The default reference transaction deadline is 3 seconds. Callers may request a larger
bounded deadline for live pages, but those runs are reported outside the three-second
reference SLA. Focus and target activation keep their own shorter child deadlines even
when the parent transaction permits more time.

## 11. Failure and recovery contract

Failure results contain operational state only:

```text
code=AMBIGUOUS_ELEMENT
failed_step=3
completed_steps=2
elapsed_ms=487
retry_safe=true
snapshot=nexample
```

The stable fields are:

- error code;
- failed step index;
- completed step count;
- elapsed milliseconds;
- whether one retry is safe;
- latest safe snapshot when available.

The skill may make one compact observation and one retry only when
`retry_safe=true`. It must not enter an unbounded `tree`/`find` loop, replay an
uncertain mutation, or change providers.

## 12. Privacy and telemetry

Structured telemetry records:

- tool name;
- total, queue, resolution, activation, observation, action, and wait durations;
- provider scan and node counts;
- cache/single-flight status;
- completed step count;
- returned and original output bytes;
- stable error code.

It never records arguments, URLs, queries, selectors, titles, labels, page content,
JavaScript, file paths, typed values, or comment text. Successful `execute` output is
at most 2 KiB. `observe_target` output is at most 4 KiB. The global formatter remains
the hard outer boundary (`src/agent_eyes/result_format.py:17-29`,
`src/agent_eyes/result_format.py:167-216`).

## 13. Performance gates

| Metric | Gate |
|---|---:|
| Known-task MCP calls | 1 |
| Exploratory-task MCP calls | at most 2 |
| Full provider scans before first mutation | at most 1 |
| Agent Eyes transaction median | at most 1 second |
| Agent Eyes transaction p95 | at most 3 seconds |
| Successful execute output | at most 2 KiB |
| observe_target output | at most 4 KiB |
| Full tool catalog | at most 16 KiB |
| Implicit shadow fallbacks | 0 |
| Fixed orchestration sleeps | 0 |
| Preloaded reference-agent median | at most 3 seconds, stretch |

The tool-catalog limit preserves the current context gate
(`tests/test_token_budget.py:10-21`). Adding tools therefore requires compacting the
catalog rather than simply increasing its budget.

## 14. Test design

### 14.1 Contract and unit tests

- Existing schemas and dispatch continue to work.
- Unknown operations, excess steps, multiple targets, loops, invalid references,
  multiple consequences, and non-final consequences fail validation.
- Snapshot locators make no native provider call.
- One transaction performs one inventory, one activation, and at most one full initial
  observation.
- Child deadlines remain bounded by their local limits.
- Mutations execute once.
- Every success and uncertainty path advances or invalidates revision state.
- Telemetry and failures contain none of the forbidden values.

### 14.2 Bitbucket-like integration fixture

The fixture includes virtualized diff rows, a hover-revealed inline-comment control,
an asynchronously inserted editor, typing, submission, and posted-state verification.
It asserts one call for the known path, at most two for discovery, one initial provider
scan, bounded output, no implicit shadow access, and no intermediate tree returned to
the model.

### 14.3 Stress and cross-platform tests

- Cancellation at every transaction boundary.
- Concurrent foreground submissions and queue deadlines.
- Focus, tab, window, and revision changes during execution.
- Repeated execution without snapshot, native-reference, worker, or memory leaks.
- Linux, macOS, Windows, and Python 3.10-3.14 retain the existing CI matrix
  (`.github/workflows/test.yml:36-71`).

### 14.4 Performance evidence

CI deterministically gates calls, scans, output, safety, and fixed sleeps. A reference
Mac runs 30 warm real-accessibility samples for the absolute median and p95 gate. A
reference model/client runs the preloaded natural-language journey. Live Bitbucket
testing is opt-in and uses only an explicitly authorized disposable PR.

## 15. Compatibility and delivery

- Existing tools remain callable with their current contracts.
- `observe_target` and `execute` are additive.
- The installed skill prefers the fast path and uses primitives only for bounded
  recovery or unsupported workflows.
- Normal browser work remains foreground/native and never probes shadow providers.
- No broker or new mandatory browser dependency ships in v0.10.0.
- Setup updates every selected client and installed skill through the existing
  persistent launcher flow.

Delivery order:

1. Capture the failure metrics and add the deterministic regression fixture.
2. Write transaction contract, safety, deadline, and privacy tests.
3. Implement the transaction state machine and compact observation.
4. Add revision invalidation and bounded inventory reuse.
5. Add structured redacted telemetry.
6. Update the skill, README, MCP reference, and installed templates.
7. Run lint, security, dependency, unit, contract, stress, and benchmark gates.
8. Build the wheel and source distribution.
9. Remove the local installed copy and install the exact built wheel.
10. Restart configured clients and rerun installed-artifact benchmarks.
11. Publish v0.10.0 only after every release-blocking gate passes.

The existing publish workflow continues to require full CI, reproducible distributions,
metadata validation, and isolated wheel smoke testing (`.github/workflows/publish.yml:1-102`).

## 16. Broker decision

A per-user broker remains the next measured boundary, not a prerequisite. It will be
designed only if cold-start or cross-client measurements fail p95 at most 3 seconds
after the transaction fast path is complete. This avoids adding authenticated local
transport, daemon lifecycle, and platform installer complexity without evidence.

## 17. Acceptance criteria

The design is complete when implementation evidence proves all of the following:

- A known Bitbucket-like inline-comment journey completes through one `execute` call.
- The discovery path uses one `observe_target` and one `execute` call.
- No transaction performs more than one full provider scan before mutation.
- PID-based repeated search does not appear in the fast path.
- A focus failure cannot consume the parent transaction deadline.
- No fixed orchestration sleep or implicit shadow fallback occurs.
- Agent Eyes median is at most 1 second and p95 is at most 3 seconds on the reference
  run.
- Tool results and telemetry satisfy the privacy and byte budgets.
- Existing tool contracts and supported client setup remain compatible.
- The exact installed wheel passes the same contract, stress, and benchmark gates.

## 18. Approved decisions

The user approved the following on 2026-07-22:

1. Backward-compatible additive tools rather than a breaking redesign.
2. Agent Eyes median at most 1 second and p95 at most 3 seconds as release gates.
3. A full preloaded reference-agent median at most 3 seconds as a stretch target.
4. A bounded transaction executor with compact observation fallback.
5. The architecture, safety contract, runtime/cache policy, benchmark model, and
   rollout described in this document.
