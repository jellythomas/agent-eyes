# Agent Eyes v2: Native-First Computer Use and Guided Setup

**Date:** 2026-07-17
**Status:** Active architecture; bootstrap/native-inventory/event slices implemented
**Scope:** Cross-platform foreground computer use, browser reuse, event-driven execution, compact MCP state, and first-run setup

## Outcome

Agent Eyes remains a model-independent MCP server that can inspect and operate the user's computer. Its default behavior is foreground, native, and browser-agnostic:

1. Inspect the current desktop and every browser window/tab exposed by the native accessibility provider.
2. Reuse and focus the best existing tab when it matches the task.
3. Open a new tab only when no suitable tab exists.
4. Use shadow/background automation only when the caller explicitly requests it.
5. Wait on observable state changes instead of fixed sleeps.
6. Keep state output compact and request targeted trees instead of repeatedly returning complete trees.

The first-run experience is one guided command:

```text
uvx agent-eyes setup
```

`uvx` is only the bootstrap. Successful setup installs a persistent Agent Eyes launcher and configures MCP clients to use that launcher.

## Verified Historical Baseline and Current Status

The initial v0.8 audit found runtime self-installation, stale Boolean readiness, transient `uvx` client entries, unsafe config composition, Chrome-first browser routing, mutable tab indices, fixed polling delays, and platform dependency drift. Those observations were baseline defects, not descriptions of the current tree.

| Area | Current implementation | Remaining architecture work |
|---|---|---|
| Startup/setup | Lightweight CLI, persistent current-version launcher, live readiness, atomic locked state, degraded handshake, no package installation in `serve` | Signed/self-contained platform bundles if Python packaging becomes insufficient |
| Client config | All-target preflight, JSON/JSONC preservation, symlink-target writes, unique backups, atomic application | Add more client-native config adapters as clients appear |
| Browser foreground | Native all-browser/window/tab inventory, intent ranking, stable provider IDs, reuse-first policy, OS default-browser fallback | Exact inactive-tab URL/DOM metadata via the shared bridge |
| Shadow mode | Explicit-only CDP/DOM entry; normal tools do not probe it | Browser-neutral BiDi provider and provider conformance suite |
| Completion | macOS AXObserver, Windows UIA/MTA, Linux AT-SPI, CDP lifecycle/AX events; bounded fallback only when registration is unavailable | Long-lived per-user observer broker and revision/delta streams |
| Token use | Compact context, bounded tree rows, normalized field lengths, targeted wait/find/subtree flow | Revision cursors and true incremental deltas |

## Architecture

```text
MCP client
  -> thin stdio server (always launchable; never installs packages)
     -> current in-process capability router
     -> future per-user Agent Eyes broker
        -> live desktop/window/tab inventory
        -> capability router
           -> native accessibility provider (default foreground)
           -> browser bridge provider (open-tab metadata + DOM events)
           -> visual provider (on-demand fallback)
           -> BiDi/CDP provider (explicit shadow/background only)
        -> revisioned element/tab registry
        -> event subscriptions and condition waiters
```

### Thin MCP server

The stdio process owns MCP framing only. It must start with core dependencies, avoid network or package-manager work, and never write diagnostics to stdout. It exposes a compact readiness/status result even if runtime providers are missing.

### Per-user broker (next performance boundary)

One broker should own expensive OS observers, browser connections, caches, and input serialization for every MCP client. This avoids rebuilding a full UI tree and reopening browser connections per request. Today, persistent CDP sessions are reused inside one MCP process while native observers are scoped and cleaned up per wait. Moving those resources behind a local authenticated broker is the next material cross-client latency improvement; it is not required for the native-first behavior already implemented.

### Capability router

Foreground operations choose the cheapest provider that can satisfy the requested capability. Browser brand is data, not a routing policy. CDP and WebDriver BiDi are optional shadow providers; they are not the normal fallback for a browser that is already open.

### Browser bridges

A shared WebExtension core reports exact tabs/URLs, navigation state, DOM/accessibility deltas, and action completion. The wire protocol and most JavaScript are browser-neutral. Chromium and Firefox still need different extension/native-host manifest fields and install locations. Safari uses the same WebExtension resources but packages native messaging through an Apple app/extension container. These are thin distribution adapters, not browser-specific MCP tools.

No current package provides transparent foreground attachment to every already-open browser, including its authenticated tabs, without browser or user opt-in. OS accessibility is therefore the universal foreground plane. WebExtensions are the metadata/event enrichment plane, and WebDriver BiDi/CDP remain explicit remote-control planes.

WebDriver BiDi is the best standards-based direction for optional shadow automation, but it is still a W3C Working Draft and establishes a remote-control session. ChromeDriver implements WebDriver and BiDi; Firefox's Remote Agent implements BiDi and requires explicit enablement under a loopback-oriented security model. Firefox removed its CDP option in Firefox 141, another reason not to make CDP the cross-browser core.

### Event-driven execution

Each action captures a pre-action revision, dispatches once, then waits for one of:

- the requested element/window/tab condition;
- an accessibility or DOM revision that invalidates the condition;
- navigation lifecycle completion;
- provider disconnect or target disappearance;
- a monotonic deadline.

Fixed sleeps are removed from orchestration. Physical input backends may retain tiny key-down/up, double-click, paste, or drag spacing because those delays define the input gesture; they are never used to guess whether a page or application finished loading. A short debounce is permitted only to coalesce an event burst, never as the definition of success.

## Browser Selection Contract

```text
refresh live inventory
  -> score all open tabs against intent
     -> exact URL/origin/title/task match: focus and reuse
     -> relevant app/site match: focus and reuse
     -> no suitable match: open a new foreground tab
     -> explicit background=true: use an available shadow provider
```

The result includes the chosen browser, window, tab, provider, and reason. Tab identity uses stable provider-qualified IDs, not a mutable list index. Generation/revision metadata is a future broker capability.

## Readiness and Setup State Machine

```text
BOOTSTRAP
  -> CHECKING
     -> READY
     -> DEGRADED
     -> SETUP_REQUIRED
     -> PERMISSION_REQUIRED

SETUP_REQUIRED
  -> PLAN
  -> CONSENT
  -> INSTALLING
  -> PERMISSION_REQUIRED
  -> INITIALIZING_CLIENTS
  -> VERIFYING
  -> READY | DEGRADED | SETUP_REQUIRED
```

Readiness is derived from live capability probes. A versioned manifest caches results only to accelerate launch. It records schema and Agent Eyes versions, executable/Python identity, platform/session type, provider versions, permissions, browser host registrations, broker protocol version, remediation, and last verification time.

The user-visible states are:

- `ready`: all selected required capabilities work.
- `degraded`: core foreground computer use works; an optional provider does not.
- `setup_required`: a required dependency or persistent launcher is missing.
- `permission_required`: software is present, but a user-controlled OS/browser permission is missing.

## CLI Contract

```text
agent-eyes setup [--yes] [--json]
agent-eyes doctor [--json] [--verbose]
agent-eyes install [--yes]
agent-eyes init [--yes] [--client CLIENT]
agent-eyes serve
```

- No arguments remain an alias for `serve` for existing MCP configurations.
- `doctor` is read-only and probes actual providers.
- `install` prints an exact plan, asks before mutation, installs a persistent isolated tool, and guides privilege/permission boundaries.
- `init` previews and safely updates detected MCP client configurations. It keeps other MCP servers by default.
- `setup` composes doctor, install, permissions, init, and final verification as an idempotent/resumable flow.
- Client-specific slash commands are convenience wrappers around this CLI, never the canonical implementation.

## Dependency and Distribution Policy

1. The base wheel contains everything required to launch the MCP and doctor commands.
2. Pure-Python platform dependencies use PEP 508 platform markers.
3. Heavy visual/OCR models, developer toolchains, and shadow providers remain optional.
4. End users do not install Node/WXT/Rust to use prebuilt browser bridges or native helpers.
5. Current distribution uses persistent `uv tool install` on macOS/Windows and a system-site-capable `pipx` installation on Linux so distro PyGObject/AT-SPI remains visible.
6. A later consumer release can ship signed per-platform broker bundles behind the same CLI/protocol.
7. Setup never silently uses sudo, clicks OS permission prompts, mutates a transient `uvx` environment, removes competing tools, or executes arbitrary downloaded shell code.

References:

- [uv tools](https://docs.astral.sh/uv/concepts/tools/)
- [Python dependency specifiers](https://packaging.python.org/en/latest/specifications/dependency-specifiers/)
- [PyGObject installation](https://pygobject.gnome.org/getting_started.html)
- [Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [Firefox native manifests](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests)
- [Safari WebExtensions](https://developer.apple.com/documentation/safariservices/creating-a-safari-web-extension)
- [Safari native-app/WebExtension messaging](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension)
- [Cross-browser WebExtension incompatibilities](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Chrome_incompatibilities)
- [WebDriver BiDi, W3C Working Draft 1 June 2026](https://www.w3.org/TR/webdriver-bidi/)
- [ChromeDriver WebDriver/BiDi implementation](https://developer.chrome.com/docs/chromedriver)
- [Firefox Remote Agent security](https://firefox-source-docs.mozilla.org/remote/Security.html)
- [Firefox remote protocol status](https://firefox-source-docs.mozilla.org/remote/index.html)
- [macOS AXObserver notifications](https://developer.apple.com/documentation/applicationservices/1462089-axobserveraddnotification)
- [Windows UI Automation event threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
- [GNOME AT-SPI EventListener](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.EventListener.html)
- [Chrome accessibility events](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)

## Safety and Recovery

- Config writes validate existing JSON and fail closed on corruption.
- Every changed config receives a unique rollback copy.
- State/config writes are atomic and protected against concurrent setup processes.
- Installation is split into plan and apply phases.
- Interrupted setup resumes at the first unverified phase.
- A missing optional bridge/visual provider produces `degraded`, not total failure.
- MCP initialization shows one actionable setup instruction; it is not prepended repeatedly to normal tool responses.

## Token and Latency Policy

- Orientation returns compact inventory summaries rather than full trees.
- Tree calls support bounded output and targeted subtree requests; cursors and true deltas are future broker capabilities.
- Stable IDs let actions refer to cached elements without restating content.
- Event completion returns only changed state plus action outcome.
- Visual frames are captured only when structured providers are insufficient.
- Normal `serve` startup performs no network work.

## Delivery Slices

### Slice 1: Safe bootstrap and readiness — implemented

- CLI command surface and persistent launcher plan.
- Live readiness probes and versioned atomic manifest.
- No runtime package installation during MCP import/startup.
- Truthful MCP status/setup instructions.
- Safe MCP config generation using the persistent executable.
- Guided README and CLI tests.

### Slice 2: Live inventory implemented; broker pending

- Native browser/window/tab inventory and stable provider IDs are implemented.
- OS event observers are implemented and scoped per wait.
- Per-user broker lifecycle, authenticated local transport, shared revision cache, and global action serialization remain.

### Slice 3: Native cross-browser foreground implemented; exact-metadata bridge pending

- Shared extension protocol and prebuilt Chromium/Firefox adapters.
- Safari packaging adapter.
- All-open-tab inventory, scoring, focus/reuse, and new-tab fallback.

### Slice 4: Condition engine implemented; deltas pending

- Event-backed native waits, activation completion, accessibility matching, and navigation completion are implemented.
- Orchestration polling sleeps are removed; compatibility fallback is adaptive and deadline-bounded.
- Revisioned tree cache, bounded deltas, and compact MCP resources remain.

### Slice 5: Visual and explicit shadow providers

- On-demand OS capture/OCR fallback.
- Explicit WebDriver BiDi/CDP background mode.
- Provider conformance, resilience, and performance benchmarks.

## Acceptance Criteria

- A fresh bootstrap can run `uvx agent-eyes setup` and receive a guided, resumable flow.
- An unprepared MCP still initializes and explains one exact recovery command.
- Starting `agent-eyes serve` never installs dependencies or contacts a package index.
- `doctor --json` reports each capability and remediation from live checks.
- Existing browser sessions are the default foreground targets; Chrome remote debugging is never required for normal operation.
- README commands and configuration examples are tested in CI.
- Core setup/readiness unit and integration tests pass on Python 3.10–3.13 and the supported OS matrix.
