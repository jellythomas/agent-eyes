# Agent Eyes MCP tool reference

**Documented release:** Agent Eyes 0.10.0

Agent Eyes exposes local computer use through the Model Context Protocol (MCP)
over stdio. It uses operating-system accessibility and real input for normal
foreground work. Background browser protocols are used only when a caller
explicitly selects shadow mode.

For installation and MCP client setup, start with the
[project README](../../README.md). For terminal commands, see the
[CLI reference](agent-eyes-cli.md).

## MCP surface

Agent Eyes exposes MCP tools only. It does not expose MCP resources, resource
templates, or prompts.

| Platform | Tool count | Platform-only differences |
|---|---:|---|
| macOS | 30 | Includes `app`, `window`, and the Apple Events `shadow` compatibility tool. |
| Windows | 27 | Omits `app`, `window`, and `shadow`. |
| Linux | 27 | Omits `app`, `window`, and `shadow`. |

All tool argument objects reject unknown properties. JSON-schema bounds and
schema-encoded argument combinations are enforced before dispatch. Handler-only
semantics, including URL schemes, non-empty batches, provider compatibility, and
action-specific conditions, are checked before the affected mutation.

Tool index:

- Transaction fast path: [`observe_target`](#observe_target),
  [`execute`](#execute)
- Orientation: [`status`](#status), [`context`](#context),
  [`list_apps`](#list_apps), [`focused`](#focused)
- Structured UI: [`tree`](#tree), [`subtree`](#subtree), [`find`](#find),
  [`pierce`](#pierce)
- Input: [`click`](#click), [`type`](#type), [`press_key`](#press_key),
  [`hover`](#hover), [`scroll`](#scroll), [`drag`](#drag),
  [`fill_form`](#fill_form), [`upload`](#upload)
- Browser/web: [`list_tabs`](#list_tabs), [`web_tree`](#web_tree),
  [`navigate`](#navigate), [`js`](#js), [`new_tab`](#new_tab),
  [`close_tab`](#close_tab), [`dialog`](#dialog), [`wait`](#wait)
- macOS-only control: [`app`](#app-macos), [`window`](#window-macos),
  [`shadow`](#shadow-macos-compatibility)
- Readiness compatibility: [`install_check`](#install_check)

## Execution modes

### Foreground mode (default)

Foreground mode uses the platform's native accessibility provider and physical
input backend:

| Platform | Observation | Input |
|---|---|---|
| macOS | AXUIElement | Quartz CGEvent |
| Windows | Microsoft UI Automation | Win32 SendInput |
| Linux/X11 | AT-SPI2 | XTest |
| Linux/Wayland | AT-SPI2 where available | Compositor/session dependent; doctor reports unsupported input honestly |

Browser foreground work scans accessibility-visible tabs across accessible
browsers. It does not require Chrome remote debugging. `list_tabs` inventories
targets; `navigate` and `new_tab` reuse a suitable current target before opening
a new foreground tab; `close_tab` closes only an exact verified target.

### Shadow mode (explicit)

Shadow mode is background/no-focus browser automation. It is never an implicit
fallback. Start with:

```json
{
  "query": "authenticated reporting dashboard",
  "shadow": true
}
```

Pass a returned `target_id` only to operations supported by that target's
provider. Tools that act on a `web_tree` element also require that tree's
snapshot token.

| Operation | Persistent loopback CDP | Legacy loopback CDP | macOS Apple Events |
|---|:---:|:---:|:---:|
| `list_tabs(shadow=true)` | First usable inventory | First usable inventory | First usable inventory |
| `web_tree` | Read/action snapshot | Read/action snapshot | Read-only snapshot |
| `navigate`, `js` | Yes | Yes | Yes |
| `click`, `type`, `fill_form`, `upload` by snapshot | Yes | Yes | No; use `shadow` where applicable |
| `press_key`, `dialog`, `scroll`, `drag`, `close_tab` | Yes | Yes | No; use `shadow` for key/scroll compatibility |
| `wait`, `pierce` | Yes | No | No |
| `execute(target.mode="shadow")` | Yes | No | No |
| `new_tab(shadow=true)` | No | Yes | No |
| `shadow` compatibility tool | No | No | Yes |

`list_tabs(shadow=true)` returns the first usable shadow-provider inventory; it
does not merge all shadow providers. It also includes native inventory when the
native provider is available. `web_tree`, `js`, `dialog`, `upload`, and `pierce`
require literal `shadow: true`; they cannot be called as foreground tools.

## Targets, snapshots, and element IDs

`target_id` identifies a browser target. A foreground ID is an opaque, provider-qualified live handle,
for example:

```text
native:28468:w0:t1:ra39c14bb6193f72e
```

The handle must not be parsed or reconstructed, converted into a CDP tab index,
or replaced by a mutable list position. Reuse it only while its short live lease
is current. Refresh `list_tabs` after navigation, closing a tab, replacement,
reordering, or a stale-target error.

Apple Events target IDs are provider-qualified. CDP target IDs are raw,
provider-owned identifiers and may not carry a prefix. An Apple Events ID cannot
be passed to a CDP-only operation; use the compatibility table above.

Observation tools such as `tree`, `focused`, `subtree`, `find`, `web_tree`, and
`pierce` return a snapshot token with local numeric element IDs. A snapshot is
immutable and bound to one provider, mode, target, connection generation, and
document revision. Pass the same snapshot whenever an action exposes that
parameter. Legacy foreground `click` and `type` calls can resolve an unqualified
ID from the current registry, but snapshot qualification is safer; shadow
element actions require it.

Snapshots are process-local, live for at most 60 seconds, and bounded to 32
stored snapshots with at most 500 elements each. A mutation can invalidate its
originating snapshot sooner. On `STALE_SNAPSHOT`, observe again rather than
reusing an old ID.

## Input and result limits

| Limit | Value |
|---|---:|
| Complete JSON argument payload | 256 KiB |
| Typed/form-field text | 16,384 characters |
| JavaScript expression | 65,536 characters |
| URL | 8,192 characters |
| Query/title/name filter | 512 characters |
| `target_id` | 512 characters |
| Snapshot token | 128 characters |
| Process ID (`pid`) | 1–2,147,483,647 |
| Local element/window ID (`id`) | 0–2,147,483,647 |
| CSS selector | 2,048 characters |
| Upload path | 4,096 characters |
| Upload files | 32 |
| Form fields | 100 |
| Key modifiers | 4 |
| `observe_target` selectors | 8 |
| `execute` steps | 1–8 |
| Transaction deadline | 1–30,000 ms; 3,000 ms by default |
| `observe_target` text result | 4 KiB |
| `execute` text result | 2 KiB |
| Default text result | 16 KiB |
| Hard text-result ceiling | 64 KiB |

Oversized results contain an explicit truncation suffix. Typed text, secure
element values, JavaScript result values, URL credentials/query values, and
protected upload locations are not echoed into normal results or logs.

## Results and errors

Successful calls return one or more MCP text-content items. Tool failures return
`CallToolResult` with `isError=true` and safe text beginning with `ERROR:`.

Common error categories:

| Error | Meaning | Recovery |
|---|---|---|
| `setup_required` | Required provider/dependency is unavailable. | Run `agent-eyes setup`. |
| `permission_required` | An OS permission or service requires user action. | Follow `agent-eyes doctor --verbose`, restart the responsible launcher if needed, and retry. |
| `STALE_SNAPSHOT` | Snapshot expired, was invalidated, or belongs to an older target generation. | Re-run the relevant observation tool. |
| `STALE_TARGET` | Browser target no longer matches current inventory. | Re-run `list_tabs`. |
| `AMBIGUOUS_TARGET` | More than one target matches an unsafe shorthand. | Use the exact current ID/snapshot. |
| `MODE_MISMATCH` | Foreground and shadow records were mixed. | Observe and act in the same explicit mode. |
| `TARGET_MISMATCH` | Snapshot belongs to another provider/target. | Use the originating snapshot or observe again. |
| `ELEMENT_NOT_FOUND` | A target or locator matched no accessible element. | Correct or broaden the selector, then observe once. |
| `AMBIGUOUS_ELEMENT` | A transaction locator matched multiple elements. | Add role/name/value or a `within` scope; never choose one arbitrarily. |
| `INVALID_TRANSACTION` | A transaction violates its schema or bounded semantic rules. | Fix the request; retrying the same input cannot succeed. |
| `FOCUS_MISMATCH` | Exact foreground window/tab/element focus could not be proven. | Refresh context/inventory and retry only after verifying the target. |
| `DEADLINE_EXCEEDED` | The bounded operation did not complete in time. | Observe current state before deciding whether retry is safe. |
| `PROVIDER_BUSY` | A previous uncertain foreground operation is still settling. | Wait for it to settle, then refresh. Do not route around it. |
| `OUTCOME_UNKNOWN` | A mutation may have completed but confirmation was lost. | Observe the exact target; never replay blindly. |
| `UNSUPPORTED_CAPABILITY` | The selected provider cannot perform the requested operation. | Keep the same mode and choose a supported tool or report the gap. |
| `RESULT_TRUNCATED` | Even compact result metadata could not fit its hard result budget. | Narrow the selectors; do not request an unbounded tree. |
| `PARTIAL_FAILURE` | A batched operation completed only some items. | Inspect the reported count and refresh affected fields. |

MCP itself does not display a confirmation prompt before dispatch. The client
and user remain responsible for approving destructive actions. Agent Eyes
enforces explicit mode, exact target, snapshot, stale-state, and input safety
boundaries.

## Recommended foreground workflow

Known task:

```text
execute(target query/exact ID/PID, <=8 locate/action/expect steps)
```

Exploratory task:

```text
observe_target(all needed selectors) -> execute(exact target and snapshot)
```

Both transaction forms inventory and reuse relevant open browser targets. Only
`execute` supports an explicit `on_missing="open"` fallback. Neither probes a
shadow provider unless `execute` receives explicit `mode="shadow"` with an exact
persistent-CDP target. Use the primitive workflow only when the bounded transaction
operation set cannot express the task:

```text
context -> list_tabs(query) -> tree(pid) -> find/subtree -> action(snapshot, id)
        -> wait(pid, role/name condition) -> targeted verification
```

Use `full=true` only when a compact interactive tree is insufficient. Prefer
`find`, `subtree`, `fill_form`, and condition-specific `wait` over repeated full
trees or fixed sleeps.

## Tools

### `status`

Run live native-accessibility and input readiness checks. It never installs,
opens a browser, or probes a shadow provider.

**Arguments:** none (`{}`).

**Returns:** compact readiness/core state, unavailable capability statuses, and
one recovery command when action is required.

**Errors:** invalid arguments or an unexpected internal failure use the common
MCP error contract. Missing capabilities are normally represented in the
successful readiness report.

**Side effects:** none outside the server; no state is persisted and no UI is
changed.

```json
{}
```

### `list_apps`

List running applications with visible windows through native accessibility.

**Arguments:** none (`{}`).

**Returns:** application name, PID, frontmost marker, and a bounded set of window
titles, or a successful empty-inventory message. Use the PID with `tree`.

**Errors:** native-provider setup/permission failures and unexpected provider
errors use the common error contract.

**Side effects:** none; this is a foreground accessibility inventory read.

```json
{}
```

### `tree`

Inspect one application through foreground native accessibility and create an
actionable snapshot.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `pid` | yes | integer | Process ID, 1–2,147,483,647. |
| `max_depth` | no | integer, `10` | Initial request depth, 0–20. A sparse browser tree may automatically deepen to 20. |
| `interactive_only` | no | boolean, `true` | Return compact interactive rows. |
| `max_items` | no | integer, `80` | 1–200 interactive rows. |
| `full` | no | boolean, `false` | Return a nested full tree and override compact mode. |
| `timeout` | no | number, `5.0` | Total deadline in seconds, 0–30. |

**Returns:** `snapshot=<token>` plus local `[id]` rows, a successful no-actionable
result that still includes the snapshot, or a bounded nested tree.

**Errors:** missing/invalid PID, unavailable native access, provider deadline,
or provider failure.

**Side effects:** creates a process-local snapshot; it does not change the UI.

```json
{
  "pid": 28468,
  "interactive_only": true,
  "max_items": 80
}
```

### `find`

Search a loaded foreground snapshot or refresh a PID and search it.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `pid` | conditional | integer | Refresh this process before searching, 1–2,147,483,647. Supply `pid` or `snapshot`. |
| `snapshot` | conditional | string | Foreground snapshot token, at most 128 characters. Supply `snapshot` or `pid`. |
| `role` | conditional | string | Role filter, at most 128 characters. |
| `name` | conditional | string | Name/title filter, at most 512 characters. |
| `value` | conditional | string | Value filter, at most 512 characters. |
| `match` | no | string, `contains` | `contains`, `exact`, `prefix`, or `suffix`. |

At least one of `role`, `name`, or `value` is required.

**Returns:** a derived snapshot, total count, and up to 20 displayed matching
elements. No matches is a successful result without a new snapshot.

**Errors:** missing filter/source, stale or mismatched snapshot, unavailable
native access, or provider failure.

**Side effects:** may refresh the named PID and creates a derived process-local
snapshot; it does not change the UI.

```json
{
  "pid": 28468,
  "role": "button",
  "name": "Save",
  "match": "exact"
}
```

### `click`

Click an element from `tree`/`web_tree`, or click exact screen coordinates.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `id` | conditional | integer | Element ID, 0–2,147,483,647. Supply `id` or the complete `x`/`y`/`pid` coordinate form, never both. |
| `snapshot` | conditional | string | Originating tree/web-tree snapshot, at most 128 characters. Required with `shadow=true`; strongly recommended with foreground `id`. |
| `x`, `y` | coordinate form | integers | Screen coordinates from -1,000,000 to 1,000,000. Both require `pid`. |
| `pid` | coordinate form | integer | Required foreground process focus/identity guard, 1–2,147,483,647. |
| `shadow` | no | boolean, `false` | Explicit shadow mode requires `id` and `snapshot`; coordinate clicks are foreground-only. |

Element and coordinate forms are exclusive. Agent Eyes rejects mixed forms before
checking readiness or sending input.

**Side effects:** one logical foreground element click can try the native `press`,
`click`, `confirm`, and `open` actions in order, then use a coordinate fallback
when bounds and input are available. A coordinate-form click first focuses and
verifies its required PID. Shadow mode dispatches one DOM action. An uncertain
action is never retried automatically.

**Returns:** a bounded confirmation naming the clicked element or coordinates.

**Errors:** incomplete or mixed action form, stale/mismatched snapshot, mode or
focus mismatch, unavailable provider/input, unsupported element, or
`OUTCOME_UNKNOWN`.

```json
{
  "id": 12,
  "snapshot": "n-example123",
  "shadow": false
}
```

### `type`

Type text into one element from a foreground or explicit-shadow observation.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `id` | yes | integer | Element ID, 0–2,147,483,647. |
| `text` | yes | string | Text to type, at most 16,384 characters. |
| `snapshot` | conditional | string | Originating snapshot, at most 128 characters. Required with `shadow=true`; strongly recommended for foreground actions. |
| `shadow` | no | boolean, `false` | Use the explicit shadow path; requires `snapshot`. |

**Returns:** outcome and typed character count; the text itself is not echoed. If
input was dispatched but post-input verification disagrees, the tool returns a
successful text result beginning `WARNING:` rather than an MCP error.

**Errors:** stale/mismatched snapshot, mode/focus mismatch, secure or unsupported
element, unavailable provider/input, or `OUTCOME_UNKNOWN`.

**Side effects:** replaces/inserts text in the selected field and invalidates
affected observation state; uncertain input is never replayed automatically.

```json
{
  "id": 4,
  "text": "quarterly report",
  "snapshot": "n-example123"
}
```

### `focused`

Return the currently focused foreground UI element.

**Arguments:** none (`{}`).

**Returns:** the focused element plus a native snapshot whenever an element is
found.

**Errors:** unavailable native access or an unexpected provider failure.

**Side effects:** creates a process-local snapshot when an element is found; it
does not change focus or UI state.

```json
{}
```

### `list_tabs`

Scan and rank open browser tabs. Foreground is the default; `shadow=true`
explicitly requests protocol metadata.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `query` | no | string | Task, site, title, or URL terms; at most 512 characters. |
| `shadow` | no | boolean, `false` | Explicitly include/use shadow-provider inventory. |
| `max_results` | no | integer, `10` | Cap native results at 1–50 and, when shadow is requested, separately cap shadow results at the same value. |

**Returns:** up to `max_results` ranked/filtered native targets plus up to
`max_results` shadow targets in provider order. `query` applies only to native
ranking. Results include target IDs, browser/PID/window metadata where
available, title, sanitized URL, selection state, and reuse guidance. Native and
Apple Events IDs are provider-qualified; CDP IDs remain provider-owned raw IDs.

**Errors:** foreground native access is required unless explicit shadow mode is
requested; unexpected inventory failures use the common error contract. A
missing optional shadow provider is reported as a successful capability note.

**Side effects:** scans current inventory and updates process-local target
caches; `shadow=true` may establish a loopback provider connection but does not
focus, open, navigate, or close tabs.

```json
{
  "query": "no na rollerblade YouTube",
  "max_results": 10
}
```

### `observe_target`

Resolve and optionally activate one foreground target, scan it once, and evaluate
all discovery selectors from the same compact accessibility observation. Use this
once before `execute` when the target or locator shape is not already known.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `query` | exactly one target selector | string | Browser task, site, title, or URL terms, 1–512 characters. Open tabs are ranked and reused; a tied top match fails closed. |
| `target_id` | exactly one target selector | string | Exact provider-qualified foreground ID beginning `native:`, 1–512 characters. |
| `pid` | exactly one target selector | integer | Exact desktop process, 1–2,147,483,647. |
| `intent` | no | string, `inspect` | `inspect` preserves focus, so a selected browser tab must already be the visible native tab; `interact` activates and verifies the exact target before scanning. |
| `selectors` | no | locator array, `[]` | Evaluate 0–8 strict locator objects together from one observation. |
| `max_results` | no | integer, `10` | Return at most 1–20 matches per selector. The total count still reports all matches. |
| `deadline_ms` | no | integer, `3000` | One monotonic deadline for resolution, activation, and observation, 1–30,000 ms. |

Exactly one of `query`, `target_id`, or `pid` is required. `observe_target` is
foreground-only; it does not accept a mode or probe a shadow provider.

Each object in `selectors` supports:

| Field | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `role` | conditional | string | Accessibility role, at most 128 characters. |
| `name` | conditional | string | Accessible name/title, at most 512 characters. |
| `value` | conditional | string | Accessible value, at most 512 characters. Secure values are never returned. |
| `match` | no | string, `exact` | Case-insensitive `exact`, `contains`, `prefix`, or `suffix` matching applied to every supplied field. |
| `within` | no | string | Transaction-local alias scope, at most 64 characters. `observe_target` has no alias table, so omit this field; a nonempty scope is unavailable and fails closed. |

At least one of `role`, `name`, or `value` is required in every selector.

**Returns:** compact JSON, at most 4 KiB, with `status`, exact target identity,
activation/source metadata, immutable snapshot token/provider/generation/revision,
scan kind/node/cache metadata, and one ordered selector result per request selector.
Each selector result contains its zero-based `index`, `status` (`missing`, `unique`,
or `ambiguous`), full `total`, truncation marker, and bounded element metadata. No
match and multiple matches are discovery results, not arbitrary element choices.
For follow-up `execute`, reuse the exact returned `native:` target `id`; when the
returned target `id` begins `pid:`, pass the separate numeric target `pid`
instead. A later complete inventory replaces obsolete native live-handle leases,
so refresh rather than synthesizing or repairing an ID.

**Errors:** `INVALID_TRANSACTION` for an invalid target/selector contract;
`ELEMENT_NOT_FOUND` when the target or accessible tree is absent;
`AMBIGUOUS_TARGET` for tied/duplicate target resolution; `FOCUS_MISMATCH` when an
`intent="inspect"` browser target is not already visible or `intent="interact"`
cannot prove activation; `STALE_SNAPSHOT`,
`DEADLINE_EXCEEDED`, `UNSUPPORTED_CAPABILITY`, `RESULT_TRUNCATED`, or the common
readiness/provider errors. This operation does not mutate page data, so correct a
bad selector or refresh stale inventory before one retry; never start a repeated
observation/search loop.

**Side effects:** `intent="inspect"` performs a foreground accessibility read and
creates a process-local snapshot. `intent="interact"` also focuses the exact
foreground target. Resolution can update the short process-local target cache; it
never opens a tab or uses a background protocol.

```json
{
  "query": "BIT-482 pagination guard Bitbucket pull request",
  "intent": "interact",
  "selectors": [
    {
      "role": "button",
      "name": "Add inline comment",
      "match": "contains"
    },
    {
      "role": "textbox",
      "name": "Comment",
      "match": "contains"
    }
  ],
  "max_results": 10,
  "deadline_ms": 3000
}
```

### `execute`

Resolve one exact target and run a bounded sequence of local locate, action, and
expectation steps without a model round trip between steps. A known task should
use this as its only MCP call.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `target` | yes | object | Strict target object described below. |
| `steps` | yes | object array | 1–8 operation-specific step objects, executed in order under one deadline. |
| `expect` | no | locator object | Final unique expectation evaluated after all steps. |
| `deadline_ms` | no | integer, `3000` | One monotonic transaction deadline, 1–30,000 ms. Child operations cannot outlive it. |

The strict `target` object supports:

| Field | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `query` | exactly one selector | string | Browser task, site, title, or URL terms, 1–512 characters. Existing open targets are ranked and reused first. |
| `target_id` | exactly one selector | string | Exact target, 1–512 characters. Foreground requires a provider-qualified `native:` ID; shadow transactions require an exact persistent-CDP target from `list_tabs(shadow=true)`. |
| `pid` | exactly one selector | integer | Foreground desktop process, 1–2,147,483,647. |
| `snapshot` | no | string | Exact native snapshot from `observe_target` or persistent-CDP snapshot from `web_tree`, 1–128 characters. It is not allowed with an open-if-missing fallback. Provider/mode/target mismatch fails closed. The snapshot binds the exact target and scope, but execution always performs one fresh scoped accessibility read before locating or acting. Shadow actions revalidate the exact backend node's role and name immediately before dispatch. |
| `mode` | no | string, `foreground` | `foreground` uses native accessibility and real input. `shadow` is allowed only when explicitly requested by the user and requires one exact persistent-CDP `target_id`; legacy CDP and Apple Events transactions are unsupported. |
| `url` | conditional | string | Explicit `about`, `http`, or `https` URL, 1–8,192 characters; allowed only with a query and `on_missing="open"`. |
| `on_missing` | no | string, `fail` | `fail` stops when the selected target is absent. `open` is foreground-only and requires `query` plus `url`; it never accompanies `target_id`, `pid`, or `snapshot`. |

Exactly one of target `query`, `target_id`, or `pid` is required. Shadow mode does
not accept query ranking, PID selection, a URL, or an open fallback, and it never
falls back to another shadow provider or foreground input.

Every step has required `op`. Unknown fields are rejected, and each operation
accepts only the fields listed here:

| `op` | Required fields | Optional fields | Behavior |
|---|---|---|---|
| `locate` | `as` and at least one of `role`, `name`, `value` | `match`, `within` | Resolve exactly one element and bind it to a transaction-local alias. |
| `hover` | `ref` | `expect` | Move to one aliased element. Foreground uses the real pointer; explicit persistent-CDP shadow mode reads the exact backend node's box geometry and dispatches one protocol mouse move. |
| `click` | `ref` | `expect`, `consequence` | Dispatch one click to one aliased element. |
| `type` | `ref`, `text` | `expect`, `consequence` | Dispatch text once; an empty string is valid, and the value is never echoed. |
| `press_key` | `ref`, `key` | `expect`, `consequence` | Dispatch one key to one aliased element. |
| `scroll` | one nonzero `delta_x` or `delta_y` | `ref`, `expect` | Scroll the target or the aliased element scope. |
| `expect` | at least one of `role`, `name`, `value` | `match`, `within` | Require exactly one matching element without dispatching input. |

Step fields have these bounds and defaults:

| Field | Type/default | Constraints and meaning |
|---|---|---|
| `op` | string | `locate`, `hover`, `click`, `type`, `press_key`, `scroll`, or `expect`. |
| `as` | string | Alias, 1–64 characters, matching `[A-Za-z][A-Za-z0-9_]*`; aliases are unique and must be defined before use. |
| `ref` | string | Previously defined alias from the current observation revision, 1–64 characters. Required by element actions and optional for `scroll`; locate again before a later action after any intervening action. |
| `role` | string | Locator role, at most 128 characters. |
| `name` | string | Locator name/title, at most 512 characters. |
| `value` | string | Locator value, at most 512 characters. |
| `match` | string, `exact` | Case-insensitive `exact`, `contains`, `prefix`, or `suffix` matching. |
| `within` | string | Previously defined alias whose strict descendants form the locator scope, at most 64 characters. |
| `text` | string | Text for `type`, at most 16,384 characters. The field must be present, but may be empty. |
| `key` | string | Key for `press_key`, 1–64 characters. Transaction key steps do not accept modifier fields. |
| `delta_x`, `delta_y` | integer, `0` | Horizontal/vertical scroll delta, each from -10,000 to 10,000; they cannot both be zero. |
| `expect` | locator object | Post-action unique condition using the same `role`/`name`/`value`/`match`/`within` contract. |
| `consequence` | literal `external_write` | Allowed only on `click`, `type`, or `press_key`. Declare at most one consequential action, and place it as the last mutating step; non-mutating expectations may follow. |

Locators resolve from one transaction-local observation. Zero matches fail with
`ELEMENT_NOT_FOUND`; multiple matches fail with `AMBIGUOUS_ELEMENT`. A successful
action invalidates active alias bindings and the local view before a later condition
is checked. A later action therefore needs a new `locate` step. For a scoped
expectation, Agent Eyes re-resolves the saved `within` locator definition in the new
revision and refreshes only the affected target scope instead of returning to the
model.

Supplying a snapshot removes another model discovery round trip; it does not authorize
stale tree reuse. The snapshot binds the exact native or persistent-CDP target/scope,
then `execute` reads that live scope once before resolving the first locator. A shadow
action revalidates the exact backend node's accessible role and name immediately
before dispatch; loader and root identity alone do not prove in-document semantics.

**Returns:** success is compact JSON, at most 2 KiB. A failed or uncertain result
is MCP error text beginning `ERROR: ` followed by the same compact JSON shape. It
contains `status` (`succeeded`, `failed`, or `outcome_unknown`), stable error
`code` when present, exact `target_id`, `completed_steps`, one-based `failed_step`
when a step failed, `elapsed_ms`, `retry_safe`, `final_expectation` (`true`,
`false`, or `null`), and the latest safe `snapshot` or an empty string. Locators,
page content, URLs, queries, and typed text are not returned.

**Errors:** `INVALID_TRANSACTION` for schema, alias, field, consequence, or mode
violations; `ELEMENT_NOT_FOUND` or `AMBIGUOUS_ELEMENT` for unsafe locators;
`AMBIGUOUS_TARGET`, `TARGET_MISMATCH`, `MODE_MISMATCH`, `STALE_SNAPSHOT`, or
`FOCUS_MISMATCH` for unsafe target state; `DEADLINE_EXCEEDED`, `PROVIDER_BUSY`,
`UNSUPPORTED_CAPABILITY` (including a legacy-CDP or Apple Events shadow target),
the common readiness/provider errors, or `OUTCOME_UNKNOWN` after a dispatch whose
completion cannot be proved. Retry only when the returned `retry_safe` is true.
Never retry `OUTCOME_UNKNOWN`, and never automatically retry a transaction
containing an `external_write` consequence.

**Side effects:** foreground mode can activate an exact existing target, open the
explicit safe URL only when the query has no suitable target and `on_missing` is
`open`, and send real pointer/keyboard input. Explicit shadow mode stays on its
selected persistent-CDP target; transaction `hover` never substitutes foreground
input or another shadow provider. Each mutation is dispatched at most once;
execution stops after the first failure or uncertain outcome and invalidates
affected observation/inventory state.

```json
{
  "target": {
    "query": "BIT-482 pagination guard Bitbucket pull request",
    "mode": "foreground",
    "on_missing": "open",
    "url": "https://bitbucket.org/acme/payments/pull-requests/482"
  },
  "steps": [
    {
      "op": "locate",
      "as": "diff_row",
      "role": "group",
      "name": "src/payments/paginator.py",
      "match": "contains"
    },
    {
      "op": "locate",
      "as": "comment_button",
      "role": "button",
      "name": "Add inline comment",
      "match": "contains",
      "within": "diff_row"
    },
    {
      "op": "click",
      "ref": "comment_button",
      "expect": {
        "role": "textbox",
        "name": "Comment",
        "match": "contains"
      }
    },
    {
      "op": "locate",
      "as": "editor",
      "role": "textbox",
      "name": "Comment",
      "match": "contains"
    },
    {
      "op": "type",
      "ref": "editor",
      "text": "Please keep the pagination guard before materializing this collection."
    },
    {
      "op": "locate",
      "as": "submit_comment",
      "role": "button",
      "name": "Add comment",
      "match": "exact"
    },
    {
      "op": "click",
      "ref": "submit_comment",
      "consequence": "external_write"
    }
  ],
  "expect": {
    "role": "article",
    "name": "Please keep the pagination guard",
    "match": "contains"
  },
  "deadline_ms": 3000
}
```

### `web_tree`

Inspect a background protocol target. Prefer `tree(pid)` for normal foreground
browser work.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `target_id` | yes | string | Compatible shadow ID from `list_tabs(shadow=true)`, at most 512 characters. |
| `shadow` | yes | literal `true` | Explicit background-mode selection. |
| `max_depth` | no | integer, `5` | 0–10. |
| `interactive_only` | no | boolean, `true` | Return compact actionable rows. |
| `full` | no | boolean, `false` | Return the full bounded protocol tree. |

**Returns:** target-bound shadow snapshot and element IDs, including with a
successful no-actionable result. Apple Events fallback trees are read-only; use
the macOS `shadow` tool for compatible Apple mutations.

**Errors:** mode mismatch, missing/incompatible/stale target, unavailable shadow
provider, changing document revision, or provider failure.

**Side effects:** enables required protocol observation domains and creates a
process-local snapshot; it does not mutate page content.

```json
{
  "target_id": "9F4C2A7B3D1E6F80A5C4B2D1908E7F6A",
  "shadow": true,
  "max_depth": 5
}
```

### `navigate`

Navigate to a URL. Foreground mode reuses a matching accessible tab before
opening the URL in the default browser.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `url` | yes | string | URL with an explicit `http`, `https`, `about`, `chrome`, or `chrome-extension` scheme; must not begin `--`; at most 8,192 characters. |
| `query` | no | string | Reuse-ranking terms, at most 512 characters. |
| `reuse_existing` | no | boolean, `true` | Reuse a relevant foreground target when safe. |
| `shadow` | no | boolean, `false` | Explicit background navigation. |
| `target_id` | when shadow | string | Compatible shadow target from `list_tabs(shadow=true)`, at most 512 characters. |

**Side effects:** may focus/reuse an existing foreground target, open one new
foreground tab, or navigate one explicit shadow target.

**Returns:** confirmation that a foreground target was reused/opened or that the
selected shadow target completed navigation.

**Errors:** invalid URL, missing/incompatible target, focus/provider failure, or
`OUTCOME_UNKNOWN` when navigation may have completed.

```json
{
  "url": "https://www.youtube.com/results?search_query=no+na+rollerblade",
  "query": "no na rollerblade YouTube",
  "reuse_existing": true
}
```

### `js`

Run JavaScript in an explicit shadow target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `expression` | yes | string | JavaScript, at most 65,536 characters. |
| `target_id` | yes | string | Compatible shadow target from `list_tabs(shadow=true)`, at most 512 characters. |
| `shadow` | yes | literal `true` | Explicit background-mode selection. |

**Returns:** result type and serialized size only. Page-owned values are not
returned or logged.

**Errors:** mode mismatch, missing/incompatible target, runtime exception,
provider failure, or `OUTCOME_UNKNOWN`.

**Side effects:** executes caller-supplied JavaScript in the selected page; the
expression may mutate page state and invalidates affected observation state.

```json
{
  "expression": "document.title",
  "target_id": "9F4C2A7B3D1E6F80A5C4B2D1908E7F6A",
  "shadow": true
}
```

### `press_key`

Press a key with optional modifiers in the foreground or an explicit shadow
target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `key` | yes | string | One character or a named key, at most 64 characters. Named keys include `Enter`, `Tab`, `Escape`, `Backspace`, `Delete`, `ArrowUp`, `ArrowDown`, `ArrowLeft`, `ArrowRight`, `Home`, `End`, `PageUp`, `PageDown`, `F1`–`F12`, and `Space`. |
| `modifiers` | no | string array | At most four names. Accepted aliases are `Alt`/`Option`, `Ctrl`/`Control`, `Meta`/`Cmd`/`Command`, and `Shift` (case-insensitive). |
| `pid` | no | integer | Optional foreground target process, 1–2,147,483,647. |
| `shadow` | no | boolean, `false` | Explicit background key input. |
| `target_id` | when shadow | string | Required persistent/legacy CDP target, at most 512 characters. |

**Returns:** confirmation of the key/modifier combination sent.

**Errors:** missing key, focus mismatch, unavailable input/provider,
missing/incompatible shadow target, or `OUTCOME_UNKNOWN`.

**Side effects:** may focus the supplied foreground PID, sends real foreground
key input or explicit CDP key input, and invalidates affected observation state.

```json
{
  "key": "Enter",
  "modifiers": [],
  "pid": 28468
}
```

### `wait`

Wait for a named/role-matched element. Foreground waits use OS notifications
first and bounded adaptive checks only when subscription is unavailable.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `role` | conditional | string | Role condition, at most 128 characters. |
| `name` | conditional | string | Name condition, at most 512 characters. |
| `timeout` | no | number, `5` | Total deadline in seconds, 0–60. |
| `pid` | foreground | integer | Foreground process to observe, 1–2,147,483,647. |
| `shadow` | no | boolean, `false` | Explicit persistent-CDP wait. |
| `target_id` | when shadow | string | Persistent-CDP target from inventory, at most 512 characters. |

At least one of `role` or `name` is required.
Foreground `pid` and shadow `target_id` forms are mutually exclusive.

**Returns:** a new actionable snapshot when found. Timeout is an MCP error, not
a successful empty result.

**Errors:** missing condition/PID/target, incompatible or unavailable provider,
document change during shadow wait, or timeout.

**Side effects:** subscribes to native/protocol events and creates a process-local
snapshot on success; it does not use a fixed sleep or mutate UI state.

```json
{
  "pid": 28468,
  "role": "button",
  "name": "Continue",
  "timeout": 10
}
```

### `new_tab`

Reuse a matching foreground tab or open one new URL. Shadow mode explicitly
creates a background protocol target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `url` | no | string, `about:blank` | URL with an explicit `http`, `https`, `about`, `chrome`, or `chrome-extension` scheme; must not begin `--`; at most 8,192 characters. |
| `query` | no | string | Reuse-ranking terms, at most 512 characters. |
| `reuse_existing` | no | boolean, `true` | Reuse a strong foreground match before opening. |
| `shadow` | no | boolean, `false` | Explicitly create a background target. |

**Side effects:** foreground mode may focus one exact existing tab or open one
URL in the operating system's default browser. If activation of a matching tab
cannot be verified, Agent Eyes fails closed rather than opening a duplicate.

**Returns:** reuse confirmation, new shadow target ID, or confirmation that the
default browser opened the URL.

**Errors:** invalid URL, unavailable provider, unverified matching-target
activation, creation failure, or `OUTCOME_UNKNOWN`.

```json
{
  "url": "https://github.com/jellythomas/agent-eyes",
  "query": "jellythomas agent-eyes GitHub",
  "reuse_existing": true
}
```

### `close_tab`

Close one exact foreground or shadow browser target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `target_id` | conditional | string | Exact current compatible target, at most 512 characters; required for shadow and preferred for foreground. |
| `title` | conditional | string | Unique case-insensitive foreground title substring, at most 512 characters. |
| `shadow` | no | boolean, `false` | Explicitly close a background target. |

Foreground mode requires `target_id` or a uniquely matching title. It rescans
and verifies the owning window and selected tab before sending the close key.

**Returns:** confirmation that the exact foreground tab or shadow target closed.

**Errors:** stale/ambiguous/incompatible target, unsupported browser exposure,
focus/input/provider failure, or `OUTCOME_UNKNOWN`.

**Side effects:** focuses and closes one verified foreground tab, or closes one
explicit CDP target; this is destructive, and target/observation caches are
invalidated.

```json
{
  "target_id": "native:28468:w0:t1:ra39c14bb6193f72e"
}
```

Refresh `list_tabs` after every close before operating on another tab.

### `dialog`

Accept or dismiss a JavaScript dialog in an explicit CDP target. Use
`tree`/`click` for visible native dialogs.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `target_id` | yes | string | Persistent/legacy CDP target from inventory, at most 512 characters. |
| `shadow` | yes | literal `true` | Explicit background-mode selection. |
| `accept` | no | boolean, `true` | Accept when true; dismiss when false. |
| `prompt_text` | no | string | Prompt response, at most 4,096 characters. |

**Returns:** confirmation that the dialog was accepted or dismissed.

**Errors:** mode mismatch, missing/incompatible target, unavailable provider,
no verifiable dialog, or `OUTCOME_UNKNOWN`.

**Side effects:** accepts/dismisses one JavaScript dialog and may submit the
provided prompt text; affected observation state is invalidated.

```json
{
  "target_id": "9F4C2A7B3D1E6F80A5C4B2D1908E7F6A",
  "shadow": true,
  "accept": true
}
```

### `upload`

Set files on one shadow DOM file-input element.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `id` | yes | integer | File-input element ID from `web_tree`, 0–2,147,483,647. |
| `files` | yes | string array | 1–32 paths, each at most 4,096 characters. Relative paths are canonicalized before validation. |
| `snapshot` | yes | string | Originating shadow web-tree snapshot, at most 128 characters. |
| `shadow` | yes | literal `true` | Explicit background-mode selection. |

Files must resolve to allowed regular files. Agent Eyes rejects protected roots,
credential stores, sensitive aliases, and unsafe links.

**Returns:** uploaded file count and selected element ID; file paths are not
echoed.

**Errors:** mode/snapshot/target mismatch, empty or protected/missing file list,
unsupported element/provider, or `OUTCOME_UNKNOWN`.

**Side effects:** attaches the validated regular files to one shadow file input
and invalidates the originating snapshot.

```json
{
  "id": 9,
  "files": ["/Users/you/Documents/quarterly-report.pdf"],
  "snapshot": "s-example123",
  "shadow": true
}
```

### `scroll`

Scroll through foreground OS input or an explicit shadow target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `delta_y` | no | integer, `300` | Positive down, negative up; ±1,000,000. |
| `delta_x` | no | integer, `0` | Positive right, negative left; ±1,000,000. |
| `x`, `y` | no | integers, `400` | Foreground scroll origin; ±1,000,000. |
| `pid` | no | integer | Optional foreground process guard, 1–2,147,483,647. |
| `shadow` | no | boolean, `false` | Explicit background scrolling. |
| `target_id` | when shadow | string | Required persistent/legacy CDP target, at most 512 characters. |

**Returns:** foreground direction confirmation or confirmation that the shadow
target scrolled.

**Errors:** focus mismatch, unavailable input/provider, missing/incompatible
shadow target, or `OUTCOME_UNKNOWN`.

**Side effects:** may focus the supplied foreground PID, then sends foreground
wheel input or scrolls one explicit CDP target; affected observation state is
invalidated.

```json
{
  "delta_y": 500,
  "delta_x": 0,
  "pid": 28468
}
```

### `drag`

Drag between two screen points through foreground input or within an explicit
shadow target.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `from_x`, `from_y` | yes | integers | Start coordinates, ±1,000,000. |
| `to_x`, `to_y` | yes | integers | End coordinates, ±1,000,000. |
| `pid` | no | integer | Optional foreground process guard, 1–2,147,483,647. |
| `shadow` | no | boolean, `false` | Explicit background drag. |
| `target_id` | when shadow | string | Required persistent/legacy CDP target, at most 512 characters. |

**Returns:** confirmation of the foreground coordinate pair or selected shadow
target drag.

**Errors:** missing coordinates, focus mismatch, unavailable input/provider,
missing/incompatible shadow target, or `OUTCOME_UNKNOWN`.

**Side effects:** may focus the supplied foreground PID, then performs one
foreground pointer drag or one explicit CDP drag; affected observation state is
invalidated.

```json
{
  "from_x": 420,
  "from_y": 310,
  "to_x": 760,
  "to_y": 310,
  "pid": 28468
}
```

### `fill_form`

Fill multiple fields from one native or explicit-shadow snapshot.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `fields` | yes | object array | 1–100 strict `{id, value}` objects. IDs are 0–2,147,483,647; values are at most 16,384 characters. |
| `snapshot` | yes | string | Snapshot shared by every field, at most 128 characters. |
| `shadow` | no | boolean, `false` | Must match the snapshot mode. |

**Returns:** success counts, `PARTIAL_FAILURE`, or `OUTCOME_UNKNOWN`. Values are
not echoed.

**Errors:** empty field list, stale/mismatched snapshot, unresolved fields,
`PARTIAL_FAILURE`, provider failure, or `OUTCOME_UNKNOWN`.

**Side effects:** types into fields sequentially within one serialized operation.
An error can leave earlier fields changed; execution stops after an uncertain
outcome, and affected snapshots are invalidated.

```json
{
  "fields": [
    {"id": 3, "value": "Jelly Thomas"},
    {"id": 4, "value": "jelly@example.com"}
  ],
  "snapshot": "n-example123"
}
```

### `hover`

Move the foreground pointer to an element or screen coordinates.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `id` | conditional | integer | Element ID, 0–2,147,483,647. Supply `id` or both coordinates. |
| `snapshot` | with `id` | string | Originating foreground snapshot, at most 128 characters. |
| `x`, `y` | conditional pair | integers | Screen coordinates, ±1,000,000. |

`hover` has no shadow route.
Element and coordinate forms are mutually exclusive; mixed inputs are rejected
before pointer movement.

**Returns:** confirmation naming the element ID or screen coordinates reached.

**Errors:** missing coordinates/identity, stale or mismatched snapshot, element
without bounds, focus/input/provider failure, or `OUTCOME_UNKNOWN`.

**Side effects:** may focus the element's owning app, moves the real foreground
pointer, and may change hover UI state; it does not click.

```json
{
  "id": 18,
  "snapshot": "n-example123"
}
```

### `app` (macOS)

Launch an application by name or bundle ID, or focus/quit an exact match followed
by a unique case-insensitive app-name substring. This tool is not exposed on
Windows or Linux.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `action` | yes | string | `launch`, `focus`, or `quit`. |
| `name` | yes | string | App name or bundle ID; focus/quit also allow a unique case-insensitive name substring; at most 4,096 characters. |

Ambiguous running-app matches fail closed.

**Returns:** confirmation naming the launched or quit application; focus
confirmation also includes the PID.

**Errors:** missing/ambiguous app, launch/focus/quit failure, or unavailable
macOS application provider.

**Side effects:** launches, activates, or terminates the selected macOS
application according to `action`.

```json
{
  "action": "focus",
  "name": "Safari"
}
```

### `subtree`

Expand one element from a prior native tree without fetching the entire app.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `id` | yes | integer | Element ID from the originating tree, 0–2,147,483,647. |
| `snapshot` | yes | string | Originating native snapshot, at most 128 characters. |
| `max_depth` | no | integer, `5` | 0–15 levels. |

**Returns:** a refreshed native snapshot for the expanded subtree.

**Errors:** stale/mismatched snapshot, missing element, unavailable native
provider, or provider failure.

**Side effects:** creates a refreshed process-local subtree snapshot; it does not
change the UI.

```json
{
  "id": 21,
  "snapshot": "n-example123",
  "max_depth": 5
}
```

### `window` (macOS)

List or mutate exact native application windows. This tool is not exposed on
Windows or Linux.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `action` | yes | string | `list`, `focus`, `minimize`, `close`, `move`, or `resize`. |
| `pid` | mutation | integer | Owning process returned by `window(action="list")`, 1–2,147,483,647. |
| `snapshot` | mutation | string | Window-list snapshot, at most 128 characters. |
| `id` | mutation | integer | Exact window ID from that snapshot, 0–2,147,483,647. |
| `x`, `y` | move | integers | New origin, ±1,000,000. |
| `width`, `height` | resize | integers | New size, 1–1,000,000. |

List first, then use its exact `pid`, `snapshot`, and `id` for every mutation.

**Returns:** `list` returns one distinct `pid`/`snapshot`/`id` identity tuple per
window; mutations return a bounded action confirmation.

**Errors:** missing/stale/mismatched identity, invalid geometry, provider/action
failure, or `OUTCOME_UNKNOWN`.

**Side effects:** `list` only observes; other actions focus, minimize, close,
move, or resize one exact macOS window and invalidate affected snapshots.

```json
{
  "action": "list"
}
```

Resize example:

```json
{
  "action": "resize",
  "pid": 28468,
  "snapshot": "w-example123",
  "id": 2,
  "width": 1280,
  "height": 800
}
```

### `context`

Return compact frontmost application, active window, and focused-element
orientation without traversing a full tree.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `fast` | no | boolean, `true` | Compatibility flag; compact mode is always used. |

**Returns:** a successful no-frontmost-app message, compact app/window text, or
that summary plus a snapshot for the focused element.

**Errors:** unavailable native access or an unexpected provider failure.

**Side effects:** may create a process-local snapshot for the focused element;
it does not change foreground UI state.

```json
{}
```

### `shadow` (macOS compatibility)

Run legacy Google Chrome Apple Events background actions. This tool is exposed
only on macOS and is not the universal foreground browser backend.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `action` | yes | string | `click`, `type`, `press_key`, `scroll`, `read`, or `js`. |
| `target_id` | yes | string | Apple Events target from `list_tabs(shadow=true)`, at most 512 characters. |
| `text` | conditional | string | Required for `type` and `js`; for `click`, supply `text` or `selector`; for `press_key`, defaults to `Enter`; at most 65,536 characters. |
| `selector` | conditional | string | Optional selector; `click` requires this or `text`; at most 2,048 characters. |
| `direction` | scroll only | string, `down` | `up` or `down`. |
| `amount` | no | integer, `300` | Scroll amount, ±1,000,000. |

**Returns:** bounded action confirmation, interactive-element text for `read`,
or JavaScript result type/size for `js`; typed text and page values are not
echoed.

**Errors:** unavailable Apple Events provider, stale/incompatible target,
missing action-specific arguments, target not found, or `OUTCOME_UNKNOWN`.

**Side effects:** all actions except `read` can mutate the selected Chrome tab;
affected Apple Events target state is invalidated after confirmed or uncertain
mutations.

```json
{
  "action": "read",
  "target_id": "apple-events:window-42:tab-7"
}
```

Prefer the typed tools (`web_tree`, `navigate`, `js`, and others) when their
provider supports the requested operation.

### `pierce`

Inspect elements inside one shadow root through persistent CDP.

| Parameter | Required | Type/default | Constraints and meaning |
|---|---:|---|---|
| `selector` | yes | string | Host element CSS selector, at most 2,048 characters. |
| `target_id` | yes | string | Persistent-CDP target from shadow inventory, at most 512 characters. |
| `shadow` | yes | literal `true` | Explicit background-mode selection. |

**Returns:** a target-bound snapshot and up to 500 shadow-root elements where
backend node identity can be proven. No match is a successful result without a
new snapshot.

**Errors:** mode mismatch, unavailable/incompatible persistent-CDP target,
missing host selector, changing document revision, or provider failure.

**Side effects:** queries inside the selected shadow root and creates a
process-local snapshot; it does not mutate page content.

```json
{
  "selector": "account-menu",
  "target_id": "9F4C2A7B3D1E6F80A5C4B2D1908E7F6A",
  "shadow": true
}
```

### `install_check`

Compatibility alias for `status`. New integrations should call `status`.

**Arguments:** none (`{}`).

**Returns:** the same live readiness text as `status`.

**Errors:** the same invalid-input/unexpected-failure contract as `status`;
missing capabilities are represented in the successful readiness result.

**Side effects:** none outside the server; no state is persisted and no UI is
changed.

```json
{}
```

## Security and operational notes

- MCP transport is local stdio; configure an absolute persistent launcher.
- CDP connections are limited to loopback targets.
- Agent Eyes never installs from MCP startup or a tool call.
- Exact snapshots/targets are required where identity cannot otherwise be
  proven; stale or ambiguous state fails closed.
- Foreground mutations serialize globally. Shadow mutations serialize per
  target.
- File upload blocks protected credential/configuration locations and unsafe
  aliases.
- Never place secrets in JavaScript, logs, task summaries, or verification
  output. Agent Eyes redacts its own sensitive result paths, but the MCP client
  must also handle user data appropriately.
