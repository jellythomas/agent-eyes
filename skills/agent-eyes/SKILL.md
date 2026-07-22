---
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

1. Call `context` once for desktop orientation. Call `status` only when readiness
   is unknown or a provider reports a setup/permission error.
2. Before browser work, call `list_tabs` once with concise task, site, title, or
   URL terms in `query`. It scans accessibility-visible targets across all browsers
   and returns stable provider-qualified target IDs.
3. Reuse the highest-confidence relevant target. If none exists, call `new_tab`
   with `query` and the URL; foreground reuse remains enabled by default.
4. Observe only the needed scope: `tree` for a browser/app PID, then `find` or
   `subtree` when narrower state is enough. Use `full=true` only when necessary.
5. Preserve the `snapshot=` token from every observation. Pass both `snapshot`
   and `[id]` to element actions; never combine an ID with another snapshot.
6. Call `wait` with a specific expected role/name condition. Do not add fixed sleeps
   or repeatedly fetch a full tree.
7. Verify the changed condition only. Refresh the affected target after
   navigation, `STALE_SNAPSHOT`, `STALE_TARGET`, or `FOCUS_MISMATCH`.

## Browser and target policy

- Inspect all open tabs before opening another. Opening a new tab is allowed only
  when no suitable target exists or the user explicitly requests one.
- Prefer provider-qualified `target_id` values from the latest `list_tabs` over
  titles; use a unique title only when no stable native ID is available.
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

## Failure and safety policy

- On `OUTCOME_UNKNOWN`, observe the exact target before deciding whether any retry
  is safe. Never retry a mutation blindly.
- On `PROVIDER_BUSY`, wait for the earlier uncertain foreground operation to settle,
  then refresh. Do not route around it through another input provider.
- Refresh after any successful or uncertain mutation before reusing element IDs.
- Before submit/delete/quit/close/navigation, confirm the exact current target and
  expected consequence. Do not infer user approval for destructive actions.
- Never reflect typed secrets or page values into summaries, logs, or verification.

## Token discipline

Prefer one ranked inventory, compact interactive trees, targeted `find`/`subtree`,
batched `fill_form`, and condition-specific `wait`. Avoid screenshots when the
accessibility tree is sufficient and avoid returning unchanged state.

If readiness is `setup_required` or `permission_required`, present the single
remediation from `status`. First-time users normally run
`uvx agent-eyes@latest setup`; that command checks and persistently installs the
current platform runtime, configures selected clients, and synchronizes the
canonical skill for Claude Code/Codex plus Codex metadata. Do not install
packages from inside an MCP tool call.
