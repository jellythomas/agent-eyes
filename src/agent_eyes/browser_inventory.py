"""Browser-neutral foreground tab and window inventory.

The inventory deliberately uses the operating-system accessibility provider.
It does not discover, connect to, or launch a browser debugging endpoint.
"""
from __future__ import annotations

import secrets
import sys
import re
import urllib.parse
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Mapping

from .adapters.base import AppInfo, UIElement
from .platform_utils import get_process_name, get_process_names


_BROWSER_NAMES = frozenset(
    {
        "arc",
        "brave",
        "brave browser",
        "chrome",
        "chromium",
        "dia",
        "duckduckgo",
        "firefox",
        "firefox developer edition",
        "floorp",
        "google chrome",
        "librewolf",
        "microsoft edge",
        "msedge",
        "opera",
        "opera gx",
        "orion",
        "safari",
        "safari technology preview",
        "vivaldi",
        "waterfox",
        "zen",
        "zen browser",
    }
)

_BROWSER_BUNDLE_IDS = frozenset(
    {
        "com.apple.safari",
        "com.apple.safaritechnologypreview",
        "com.brave.browser",
        "com.google.chrome",
        "com.google.chrome.canary",
        "com.kagi.kagimacossafari",
        "com.microsoft.edgemac",
        "com.operasoftware.opera",
        "com.vivaldi.vivaldi",
        "company.thebrowser.browser",
        "company.thebrowser.dia",
        "org.chromium.chromium",
        "org.mozilla.firefox",
        "org.mozilla.firefoxdeveloperedition",
        "org.mozilla.librewolf",
        "org.waterfoxproject.waterfox",
    }
)

_TAB_ROLES = frozenset({"tab", "tabitem", "page tab", "pagetab"})
_TAB_CONTAINER_ROLES = frozenset({"tabgroup", "tab group", "tablist", "tab list"})
_TAB_CHILD_ROLES = frozenset({"radio", "radiobutton", "radio button"})
_WEB_CONTENT_ROLES = frozenset(
    {
        "document",
        "document frame",
        "document web",
        "web area",
        "webarea",
    }
)

_PROCESS_BROWSER_NAMES = {
    "arc": "Arc",
    "brave": "Brave Browser",
    "brave browser": "Brave Browser",
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "dia": "Dia",
    "firefox": "Firefox",
    "google chrome": "Google Chrome",
    "librewolf": "LibreWolf",
    "microsoft edge": "Microsoft Edge",
    "msedge": "Microsoft Edge",
    "opera": "Opera",
    "safari": "Safari",
    "vivaldi": "Vivaldi",
    "waterfox": "Waterfox",
    "zen": "Zen Browser",
}

_BROWSER_IDENTITY_HINTS = frozenset(
    {
        "chromium",
        "firefox",
        "floorp",
        "ladybird",
        "librewolf",
        "mullvad",
        "safari",
        "thorium",
        "vivaldi",
        "waterfox",
    }
)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def is_browser_app(name: str, bundle_id: str = "") -> bool:
    """Return whether an application identity is recognizably browser-like."""
    normalized_name = _normalize(name)
    normalized_bundle = bundle_id.casefold().strip()
    name_tokens = set(normalized_name.split())
    return (
        normalized_name in _BROWSER_NAMES
        or normalized_bundle in _BROWSER_BUNDLE_IDS
        or normalized_name.endswith(" browser")
        or bool(name_tokens & _BROWSER_IDENTITY_HINTS)
    )


def _browser_name(app: AppInfo, *, process_name: str | None = None) -> str:
    if is_browser_app(app.name, app.bundle_id):
        return app.name

    # Windows UI Automation reports top-level window titles as AppInfo.name,
    # so resolve the executable identity by PID there. Other adapters already
    # expose stable application names; avoiding a process lookup for every
    # non-browser app keeps inventory fast.
    if sys.platform != "win32":
        return ""

    raw_process_name = (
        get_process_name(app.pid) if process_name is None else process_name
    ).removesuffix(".exe")
    process_name = _normalize(raw_process_name)
    for alias in sorted(_PROCESS_BROWSER_NAMES, key=len, reverse=True):
        if process_name == alias or f" {alias} " in f" {process_name} ":
            return _PROCESS_BROWSER_NAMES[alias]
    if is_browser_app(raw_process_name):
        return " ".join(part.capitalize() for part in process_name.split())
    return ""


def browser_name_for_app(app: AppInfo) -> str:
    """Return the cross-platform browser identity for one application record."""
    return browser_names_for_apps([app]).get(app.pid, "")


def browser_names_for_apps(apps: Iterable[AppInfo]) -> dict[int, str]:
    """Resolve browser identities with one fresh Windows process snapshot."""
    observed = tuple(apps)
    names = {
        app.pid: app.name
        for app in observed
        if is_browser_app(app.name, app.bundle_id)
    }
    if sys.platform != "win32":
        return names

    unresolved = [app for app in observed if app.pid not in names]
    process_names = get_process_names(app.pid for app in unresolved)
    for app in unresolved:
        browser_name = _browser_name(
            app,
            process_name=process_names.get(app.pid, ""),
        )
        if browser_name:
            names[app.pid] = browser_name
    return names


@dataclass
class BrowserTarget:
    """An already-open browser tab or, when unavailable, browser window."""

    browser: str
    pid: int
    title: str
    url: str = ""
    window_index: int = -1
    tab_index: int = -1
    selected: bool = False
    frontmost: bool = False
    source: str = "native"
    score: int = 0
    identity_token: str = field(
        default_factory=lambda: secrets.token_hex(8),
        repr=False,
        compare=False,
    )
    element: UIElement | None = field(default=None, repr=False, compare=False)
    window_element: UIElement | None = field(default=None, repr=False, compare=False)

    @property
    def identifier(self) -> str:
        """Return an opaque provider-qualified identity for this live observation."""
        window = max(self.window_index, 0)
        suffix = f":t{self.tab_index}" if self.tab_index >= 0 else ""
        return f"native:{self.pid}:w{window}{suffix}:r{self.identity_token}"


class BrowserQueryState(Enum):
    """Whether native evidence proves a query present, absent, or unknowable."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def extract_tab_elements(root: UIElement | None) -> list[UIElement]:
    """Extract browser-chrome tab controls without entering page content."""
    if root is None:
        return []

    tabs: list[UIElement] = []

    def visit(element: UIElement, inside_tab_container: bool = False) -> None:
        role = _normalize(element.role)
        if role in _WEB_CONTENT_ROLES:
            return

        in_container = inside_tab_container or role in _TAB_CONTAINER_ROLES
        semantic_button_tab = (
            in_container
            and role == "button"
            and _normalize(element.description) in _TAB_ROLES
        )
        if (
            role in _TAB_ROLES
            or (in_container and role in _TAB_CHILD_ROLES)
            or semantic_button_tab
        ):
            tabs.append(element)

        for child in element.children:
            visit(child, in_container)

    visit(root)
    return tabs


def _element_title(element: UIElement) -> str:
    return (element.name or element.value or element.description).strip()


def _element_url(element: UIElement) -> str:
    for value in (element.value, element.description):
        candidate = value.strip()
        if candidate.startswith(("http://", "https://", "file://", "about:")):
            return candidate
    return ""


def collect_browser_targets(
    adapter,
    *,
    tree_depth: int = 8,
    require_complete: bool = False,
    apps: Iterable[AppInfo] | None = None,
    browser_names: Mapping[int, str] | None = None,
) -> list[BrowserTarget]:
    """Inventory every visible browser process using native accessibility only.

    Tab controls are preferred. Window titles remain as conservative fallbacks
    for browsers or windows whose tab strip is not exposed by the OS provider.
    Failures are isolated per application for best-effort inventory. Callers
    that may open a missing URL can require a complete inventory so provider
    failures are never mistaken for target absence.
    """
    if adapter is None:
        return []

    if apps is None:
        try:
            list_apps = (
                getattr(adapter, "list_apps_complete", None)
                if require_complete
                else None
            )
            observed_apps: list[AppInfo] = (
                list_apps() if callable(list_apps) else adapter.list_apps()
            )
        except Exception:
            if require_complete:
                raise
            return []
    else:
        observed_apps = list(apps)

    resolved_browser_names = (
        dict(browser_names)
        if browser_names is not None
        else browser_names_for_apps(observed_apps)
    )
    targets: list[BrowserTarget] = []
    for app in observed_apps:
        browser_name = resolved_browser_names.get(app.pid, "")
        if not browser_name:
            continue

        visibility = None
        required_window_count = None
        if require_complete:
            visible_windows = getattr(
                adapter,
                "browser_app_has_visible_windows",
                None,
            )
            if callable(visible_windows):
                visibility = visible_windows(app)
            if visibility is False:
                continue
            count_required_windows = getattr(
                adapter,
                "browser_app_required_window_count",
                None,
            )
            if callable(count_required_windows):
                required_window_count = count_required_windows(app)

        app_targets: list[BrowserTarget] = []
        try:
            get_browser_trees = getattr(
                adapter,
                (
                    "get_browser_trees_complete"
                    if require_complete
                    else "get_browser_trees"
                ),
                None,
            )
            if require_complete and not callable(get_browser_trees):
                get_browser_trees = getattr(adapter, "get_browser_trees", None)
            if callable(get_browser_trees):
                trees = list(get_browser_trees(app.pid, max_depth=tree_depth))
            else:
                tree = adapter.get_tree(app.pid, max_depth=tree_depth)
                trees = [tree] if tree is not None else []
        except Exception:
            if require_complete:
                raise
            trees = []

        if require_complete and not trees:
            raise RuntimeError(
                f"browser tree inventory is incomplete for PID {app.pid}"
            )

        global_tab_index = 0
        tab_window_index = 0
        verified_tab_windows = 0
        represented_window_indices: set[int] = set()
        for provider_window_index, tree in enumerate(trees):
            tab_elements = extract_tab_elements(tree)
            if require_complete and not tab_elements:
                raise RuntimeError(
                    f"browser tab inventory is incomplete for PID {app.pid}"
                )
            for element in tab_elements:
                element.window_index = tab_window_index
                app_targets.append(
                    BrowserTarget(
                        browser=browser_name,
                        pid=app.pid,
                        title=_element_title(element),
                        url=_element_url(element),
                        window_index=tab_window_index,
                        tab_index=global_tab_index,
                        selected=any(
                            state.casefold() in {"selected", "focused", "active"}
                            for state in element.states
                        ),
                        frontmost=app.is_frontmost,
                        element=element,
                        window_element=tree,
                    )
                )
                global_tab_index += 1
            if tab_elements:
                verified_tab_windows += 1
                tab_window_index += 1
                represented_window_indices.add(provider_window_index)
            elif not require_complete and tree.name.strip():
                app_targets.append(
                    BrowserTarget(
                        browser=browser_name,
                        pid=app.pid,
                        title=tree.name.strip(),
                        window_index=provider_window_index,
                        selected=app.is_frontmost and not app_targets,
                        frontmost=app.is_frontmost,
                        source="native-window",
                        element=tree,
                        window_element=tree,
                    )
                )
                represented_window_indices.add(provider_window_index)

        if require_complete:
            visible_window_count = (
                required_window_count
                if required_window_count is not None
                else sum(bool(title.strip()) for title in app.windows)
            )
            if (
                (trees and verified_tab_windows == 0)
                or visible_window_count > verified_tab_windows
            ):
                raise RuntimeError(
                    f"browser tab inventory is incomplete for PID {app.pid}"
                )
            targets.extend(app_targets)
            continue

        for window_index, title in enumerate(app.windows):
            clean_title = title.strip()
            if not clean_title or window_index in represented_window_indices:
                continue
            app_targets.append(
                BrowserTarget(
                    browser=browser_name,
                    pid=app.pid,
                    title=clean_title,
                    window_index=window_index,
                    selected=app.is_frontmost and not app_targets,
                    frontmost=app.is_frontmost,
                    source="native-window",
                )
            )

        targets.extend(app_targets)

    return targets


def _parsed_web_url(value: str) -> urllib.parse.SplitResult | None:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed


def _normalized_host(parsed: urllib.parse.SplitResult) -> str:
    host = (parsed.hostname or "").casefold().rstrip(".")
    return host.removeprefix("www.")


def _normalized_path(parsed: urllib.parse.SplitResult) -> str:
    path = urllib.parse.unquote(parsed.path or "/")
    return path.rstrip("/") or "/"


def _url_query_score(target: BrowserTarget, query: str) -> int:
    """Score a URL intent without treating schemes or public suffixes as matches."""
    requested = _parsed_web_url(query)
    if requested is None:
        return -1

    existing = _parsed_web_url(target.url)
    if existing is not None:
        if _normalized_host(existing) != _normalized_host(requested):
            return 0
        if _normalized_path(existing) != _normalized_path(requested):
            return 0
        if requested.query and existing.query != requested.query:
            return 0
        score = 220
        if existing.scheme.casefold() == requested.scheme.casefold():
            score += 20
        if existing.query == requested.query:
            score += 20
        return score

    # Some accessibility providers expose only a tab label.  Require the
    # meaningful host label, never generic tokens such as https/com/org.
    # A title-only match cannot prove a non-root path or query is open; reuse
    # is therefore limited to root URLs where the host title is sufficient.
    if _normalized_path(requested) != "/" or requested.query or requested.fragment:
        return 0
    host_label = _normalized_host(requested).split(".")[0]
    if len(host_label) >= 3 and host_label in _normalize(target.title).split():
        return 120
    return 0


def _match_score(target: BrowserTarget, query: str) -> int:
    url_score = _url_query_score(target, query)
    if url_score >= 0:
        if url_score == 0:
            return 0
        return url_score + (8 if target.selected else 0) + (4 if target.frontmost else 0)

    phrase = _normalize(query)
    tokens = phrase.split()
    if not tokens:
        return 0

    title = _normalize(target.title)
    url = _normalize(target.url)
    browser = _normalize(target.browser)
    title_tokens = set(title.split())
    url_tokens = set(url.split())
    browser_tokens = set(browser.split())
    combined_tokens = title_tokens | url_tokens | browser_tokens
    if any(token not in combined_tokens for token in tokens):
        return 0

    score = 0
    if phrase and phrase in title:
        score += 120
    if phrase and phrase in url:
        score += 140
    score += sum(28 for token in tokens if token in title_tokens)
    score += sum(32 for token in tokens if token in url_tokens)
    score += sum(6 for token in tokens if token in browser_tokens)
    score += 60
    if target.selected:
        score += 8
    if target.frontmost:
        score += 4
    return score


def rank_browser_targets(
    targets: Iterable[BrowserTarget], query: str = ""
) -> list[BrowserTarget]:
    """Rank likely reusable targets while keeping deterministic input order."""
    ranked = [replace(target, score=_match_score(target, query)) for target in targets]
    if query.strip():
        ranked.sort(key=lambda target: target.score, reverse=True)
    return ranked


def best_browser_target(
    targets: Iterable[BrowserTarget],
    query: str,
    *,
    minimum_score: int = 60,
) -> BrowserTarget | None:
    """Return a sufficiently strong existing target match, if one exists."""
    ranked = rank_browser_targets(targets, query)
    if not ranked or ranked[0].score < minimum_score:
        return None
    return ranked[0]


def classify_browser_query(
    targets: Iterable[BrowserTarget],
    query: str,
) -> BrowserQueryState:
    """Classify absence only when every visible tab exposes title and URL evidence."""
    observed = tuple(targets)
    if best_browser_target(observed, query) is not None:
        return BrowserQueryState.PRESENT
    if not observed:
        return BrowserQueryState.ABSENT
    if any(not target.title.strip() or not target.url.strip() for target in observed):
        return BrowserQueryState.UNKNOWN
    return BrowserQueryState.ABSENT


def select_browser_target(adapter, target: BrowserTarget) -> bool:
    """Select one target using only its accessibility-provider-owned reference."""
    if target.element is None:
        return False
    if target.selected:
        try:
            if adapter.is_element_selected(target.element):
                return True
        except Exception:
            pass

    available_actions = {action.casefold(): action for action in target.element.actions}
    candidates = []
    for preferred in ("select", "press", "click", "invoke"):
        action = available_actions.get(preferred)
        if action is not None:
            candidates.append(action)
    if not candidates:
        candidates = ["select", "press"]

    for action in candidates:
        try:
            if adapter.perform_action(target.element, action):
                return True
        except Exception:
            continue
    try:
        return bool(adapter.focus_element(target.element))
    except Exception:
        return False


def activate_browser_target(adapter, input_provider, target: BrowserTarget) -> bool:
    """Bring an existing native browser target forward and select its tab.

    Kept as a synchronous compatibility helper. The async server performs these
    two provider-affine phases on separate serialized workers.
    """
    window_active = target.frontmost
    if input_provider is not None:
        try:
            if input_provider.is_available():
                window_active = bool(input_provider.activate_window(target.pid)) or window_active
        except Exception:
            pass

    if target.window_element is None:
        return False
    try:
        exact_window = bool(adapter.is_window_focused(target.window_element))
        if not exact_window:
            exact_window = bool(adapter.focus_window(target.window_element)) and bool(
                adapter.is_window_focused(target.window_element)
            )
    except Exception:
        return False
    if not window_active or not exact_window:
        return False
    if target.source == "native-window":
        return True
    if target.element is None:
        return False
    return select_browser_target(adapter, target)


def _compact(value: str, limit: int) -> str:
    one_line = " ".join(value.split())
    if len(one_line) <= limit:
        return one_line
    return f"{one_line[: limit - 1]}…"


def sanitize_url_for_display(value: str) -> str:
    """Render URL context without credentials, query values, or fragments."""
    candidate = " ".join(value.split())
    if not candidate:
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "[redacted URL]"

    scheme = parsed.scheme.casefold()
    if scheme == "about":
        return urllib.parse.urlunsplit((scheme, "", parsed.path, "", ""))
    if scheme == "file":
        return "file:///[local path redacted]"
    if scheme not in {"http", "https"} or not parsed.hostname:
        return "[redacted URL]"

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "[redacted URL]"
    netloc = f"{host}:{port}" if port is not None else host
    query = "redacted" if parsed.query else ""
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", query, "")
    )


def format_browser_targets(
    targets: Iterable[BrowserTarget],
    *,
    query: str = "",
    max_query_results: int = 10,
) -> str:
    """Render a compact, token-conscious native inventory."""
    if (
        isinstance(max_query_results, bool)
        or not isinstance(max_query_results, int)
        or max_query_results < 1
    ):
        raise ValueError("max_query_results must be a positive integer")
    all_targets = list(targets)
    browser_count = len({(target.browser, target.pid) for target in all_targets})
    lines = [
        f"Scanned {len(all_targets)} open browser targets across "
        f"{browser_count} browser processes using foreground accessibility."
    ]

    ranked = rank_browser_targets(all_targets, query)
    if query.strip():
        ranked = [target for target in ranked if target.score > 0][:max_query_results]
        if ranked:
            lines.append(f"Best reusable matches for {_compact(query, 100)!r}:")
        else:
            lines.append(f"No likely title or URL match for {_compact(query, 100)!r}.")
            return "\n".join(lines)
    else:
        if not ranked:
            lines.append("No open browser tabs or windows were visible to the native provider.")
            return "\n".join(lines)
        if len(ranked) > max_query_results:
            prioritized = [
                target for target in ranked if target.selected or target.frontmost
            ]
            prioritized_ids = {id(target) for target in prioritized}
            remainder = [target for target in ranked if id(target) not in prioritized_ids]
            ranked = (prioritized + remainder)[:max_query_results]
            lines.append(
                f"Showing {len(ranked)} targets; "
                f"{len(all_targets) - len(ranked)} additional targets omitted."
            )

    for target in ranked:
        flags = []
        if target.selected:
            flags.append("selected")
        if target.frontmost:
            flags.append("frontmost")
        state = f" ({','.join(flags)})" if flags else ""
        location = f"pid={target.pid}"
        if target.tab_index >= 0:
            location += f" tab={target.tab_index}"
        elif target.window_index >= 0:
            location += f" window={target.window_index}"
        line = (
            f"[{target.identifier}] {target.browser} {location}{state} — "
            f"{_compact(target.title or 'Untitled tab', 160)}"
        )
        if target.url:
            line += f" — {_compact(sanitize_url_for_display(target.url), 180)}"
        lines.append(line)

    lines.append(
        "Reuse the best matching foreground target; native IDs are not CDP tab_index values. "
        "Open a new tab only when no suitable match exists."
    )
    return "\n".join(lines)
