# Agent Eyes MCP tool reference

**Documented baseline:** Agent Eyes 0.9.0 plus the contract-alignment changes in
this source tree

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
| macOS | 28 | Includes `app`, `window`, and the Apple Events `shadow` compatibility tool. |
| Windows | 25 | Omits `app`, `window`, and `shadow`. |
| Linux | 25 | Omits `app`, `window`, and `shadow`. |

All tool argument objects reject unknown properties. JSON-schema bounds and
schema-encoded argument combinations are enforced before dispatch. Handler-only
semantics, including URL schemes, non-empty batches, provider compatibility, and
action-specific conditions, are checked before the affected mutation.

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
| `new_tab(shadow=true)` | No | Yes | No |
| `shadow` compatibility tool | No | No | Yes |

`list_tabs(shadow=true)` returns the first usable shadow-provider inventory; it
does not merge all shadow providers. It also includes native inventory when the
native provider is available. `web_tree`, `js`, `dialog`, `upload`, and `pierce`
require literal `shadow: true`; they cannot be called as foreground tools.

## Targets, snapshots, and element IDs

`target_id` identifies a browser target. Foreground IDs are provider-qualified
and content-fingerprinted, for example:

```text
native:28468:w0:t1:ha39c14bb6193
```

Do not convert native IDs into CDP tab indexes or rely on a mutable list
position. Refresh `list_tabs` after navigation, closing a tab, or a stale-target
error.

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
| `FOCUS_MISMATCH` | Exact foreground window/tab/element focus could not be proven. | Refresh context/inventory and retry only after verifying the target. |
| `DEADLINE_EXCEEDED` | The bounded operation did not complete in time. | Observe current state before deciding whether retry is safe. |
| `PROVIDER_BUSY` | A previous uncertain foreground operation is still settling. | Wait for it to settle, then refresh. Do not route around it. |
| `OUTCOME_UNKNOWN` | A mutation may have completed but confirmation was lost. | Observe the exact target; never replay blindly. |
| `PARTIAL_FAILURE` | A batched operation completed only some items. | Inspect the reported count and refresh affected fields. |

MCP itself does not display a confirmation prompt before dispatch. The client
and user remain responsible for approving destructive actions. Agent Eyes
enforces explicit mode, exact target, snapshot, stale-state, and input safety
boundaries.

## Recommended foreground workflow

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
  "target_id": "native:28468:w0:t1:ha39c14bb6193"
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
