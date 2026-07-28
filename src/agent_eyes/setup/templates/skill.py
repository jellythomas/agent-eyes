"""Canonical SKILL.md template for clients that support local skills."""

SKILL_MD = r"""---
name: agent-eyes
description: >
  Primary model-independent local computer-use workflow for native accessibility
  and real input across browsers and desktop apps. Use whenever an agent must
  inspect or control UI, reuse authenticated browser tabs, navigate, click, type,
  fill forms, manage apps/windows/tabs, test a visible workflow, wait for UI state,
  or perform explicitly requested background/shadow browser automation.
---

# Agent Eyes

Use foreground native automation by default. Treat background protocol access as
an explicit capability, never as a fallback for ordinary browser work.

## Execute efficiently

1. Call `status` only when readiness is unknown or a provider reports a
   setup/permission error.
2. For a known task, call `execute` once. Put the target query/exact ID/PID,
   transaction-local `locate` aliases, actions, and final expectation in that call.
   A browser query inventories and reuses open tabs across all browsers internally.
   Allow a new tab only with explicit `on_missing="open"` plus an `about`, `http`,
   or `https` URL.
3. For an exploratory task, use at most two normal-path calls: `observe_target`
   once with every needed selector, then `execute` once with its snapshot and
   exact selector. Reuse `target.id` only when it begins `native:`; for a PID
   observation, reuse `target.pid` instead.
4. Use `context`, `list_tabs`, `tree`, `find`, `subtree`, individual actions, and
   `wait` only for workflows the bounded transaction operations cannot express or
   for one safe, explicitly indicated recovery. Never enter a repeated tree/find
   search loop or add fixed sleeps.
5. Snapshots bind IDs to their originating target, but do not authorize cross-request
   tree reuse. Given a native snapshot, foreground `execute` still performs one live scoped accessibility refresh
   before locating or acting. A persistent-CDP
   snapshot triggers a fresh revision-bracketed observation, document-revision validation,
   and exact backend role/name validation before a shadow action.
   Refresh after stale state. Action `ref` aliases are revision-scoped, so add a
   new `locate` before a later action after any intervening action.

## Browser and target policy

- Inspect all open tabs before opening another. Opening a new tab is allowed only
  when no suitable target exists or the user explicitly requests one.
- Prefer a target `query` inside `observe_target`/`execute`; it performs the open-tab
  inventory without adding a separate model/tool round trip.
- Prefer stable provider-qualified target IDs from the latest `observe_target` or
  `list_tabs` over titles; use a unique title only when no stable native ID is
  available.
- Never use a positional tab index for a destructive foreground action.
- Re-read inventory between close/navigation operations because browser state and
  IDs can change.
- On platforms that expose the `window` tool, call `window(action=list)` first and
  pass its exact `snapshot` plus `[id]` to every window mutation. Use an exact or
  uniquely matching app name for `app`; stop on `AMBIGUOUS_TARGET`.
- Preserve the user's current focus unless foreground interaction requires it.

## Shadow/background policy

Use `shadow=true` only when the user explicitly requests background, no-focus, or
protocol/DOM execution. First call `list_tabs(shadow=true)`, then pass its exact
`target_id` only to tools compatible with that returned provider. Apple Events
targets support `web_tree` reads plus `navigate`, `js`, and the macOS `shadow`
compatibility tool; CDP-only actions reject them. Use the snapshot from `web_tree`
for compatible shadow element actions. If the optional provider is unavailable,
report the capability gap; do not ask to restart Chrome for normal foreground work
and do not silently replay through another provider.

For `execute`, foreground mode is the default. Set target `mode="shadow"` only for
the same explicit request and only with one exact persistent-CDP target returned by
`list_tabs(shadow=true)`. Legacy CDP and Apple Events transactions are unsupported;
use only their compatible primitive tools. Never use shadow as fallback for a
failed foreground transaction.

## Failure and safety policy

- On `OUTCOME_UNKNOWN`, observe the exact target only to report its current state.
  Never retry that transaction or mutation.
- Never retry a transaction containing `consequence="external_write"`, including
  an inline comment, submit, send, publish, or delete action.
- Retry another transaction at most once and only when its result explicitly says
  `retry_safe=true`; otherwise return the compact failure state.
- On `PROVIDER_BUSY`, wait for the earlier uncertain foreground operation to settle,
  then refresh. Do not route around it through another input provider.
- Refresh after any successful or uncertain mutation before reusing element IDs.
- Before submit/delete/quit/close/navigation, confirm the exact current target and
  expected consequence. Do not infer user approval for destructive actions.
- Never reflect typed secrets or page values into summaries, logs, or verification.

## Token discipline

Prefer one `execute` call for known work or `observe_target` then `execute` for
discovery. Keep transactions to the minimum unique locators and at most eight steps.
For unsupported work, prefer one ranked inventory, compact interactive trees,
targeted `find`/`subtree`, batched `fill_form`, and condition-specific `wait`. Avoid
screenshots when accessibility is sufficient and avoid returning unchanged state.

If readiness is `setup_required` or `permission_required`, present the single
remediation from `status`. First-time users normally run
`uvx agent-eyes@latest setup`; that command checks and persistently installs the
current platform runtime, configures selected clients, and synchronizes the
canonical skill for Claude Code/Codex plus Codex metadata. Do not install
packages from inside an MCP tool call.
"""
