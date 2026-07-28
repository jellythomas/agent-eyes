"""Shared deterministic browser fixture for transaction benchmarks and stress."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mcp.types import CallToolResult

from agent_eyes.adapters.base import UIElement
from agent_eyes.browser_inventory import BrowserTarget
from agent_eyes.coordinator import AutomationCoordinator
from agent_eyes.native_events import run_native_action_until
from agent_eyes.operation import OperationMode

FIXTURE_PID = 4_482
FIXTURE_QUERY = "BIT-482 pagination guard Bitbucket pull request"
FIXTURE_SECRET = "benchmark inline review secret 7e4d"


@dataclass(slots=True)
class FixtureCounters:
    """Monotonic counters; snapshots can be subtracted around one scenario."""

    mcp_calls: int = 0
    inventory_calls: int = 0
    activations: int = 0
    full_observations: int = 0
    scoped_observations: int = 0
    event_registrations: int = 0
    event_closes: int = 0
    dispatches: int = 0
    external_writes: int = 0
    shadow_probes: int = 0
    revisions: int = 0
    worker_calls: int = 0

    def snapshot(self) -> dict[str, int]:
        return {field: int(getattr(self, field)) for field in self.__dataclass_fields__}


def counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Return an exact non-negative delta between two fixture snapshots."""
    if before.keys() != after.keys():
        raise ValueError("fixture counter snapshots have different fields")
    delta = {name: after[name] - before[name] for name in before}
    if any(value < 0 for value in delta.values()):
        raise ValueError("fixture counters must be monotonic")
    return delta


class _InlineWorker:
    def __init__(self, counters: FixtureCounters) -> None:
        self._counters = counters
        self.active = 0

    async def run(self, call, **kwargs):
        pre_dispatch = kwargs.get("pre_dispatch")
        if pre_dispatch is not None:
            pre_dispatch()
        self._counters.worker_calls += 1
        self.active += 1
        try:
            return call()
        finally:
            self.active -= 1

    async def wait_until_idle(self) -> None:
        if self.active:
            raise RuntimeError("fixture worker remained active")

    async def aclose(self) -> None:
        await self.wait_until_idle()


class _FixtureSubscription:
    active = True

    def __init__(self, fixture: TransactionFixture) -> None:
        self._fixture = fixture
        self.generation = fixture.counters.revisions
        self._closed = False

    async def wait_for_change(self, after_generation: int, timeout: float) -> bool:
        if self._closed or timeout <= 0:
            return False
        if self._fixture.phase == "editor_pending":
            self._fixture.transition("editor_visible")
        self.generation = self._fixture.counters.revisions
        return self.generation > after_generation

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.active = False
        self._fixture._active_subscriptions -= 1
        self._fixture.counters.event_closes += 1


class TransactionFixture:
    """Bitbucket-like state machine backed only by deterministic native refs."""

    _PHASES = frozenset(
        {
            "virtualized",
            "row_visible",
            "control_visible",
            "editor_pending",
            "editor_visible",
            "typed",
            "posted",
        }
    )

    def __init__(self) -> None:
        self.counters = FixtureCounters()
        self.phase = "virtualized"
        self._active = False
        self._active_subscriptions = 0
        self._first_mutation_full_scans: int | None = None
        self._full_observations_at_reset = 0
        self._inventory_calls_at_reset = 0
        self._focused: UIElement | None = None
        self._elements: dict[int, UIElement] = {}
        self._window = UIElement(
            id=1,
            role="window",
            name="Firefox — Pull request 482",
            platform_ref=object(),
            bounds=(0, 0, 1280, 900),
        )
        self._tab = UIElement(
            id=2,
            role="tab",
            name="BIT-482 pagination guard pull request",
            platform_ref=object(),
        )
        self.target = BrowserTarget(
            browser="Firefox",
            pid=FIXTURE_PID,
            title="BIT-482 pagination guard pull request",
            url="https://bitbucket.example.test/acme/repo/pull-requests/482",
            window_index=0,
            tab_index=2,
            identity_token="0123456789abcdef",
            element=self._tab,
            window_element=self._window,
        )
        self._saved: dict[str, Any] | None = None
        self._coordinator: AutomationCoordinator | None = None
        self._native_worker: _InlineWorker | None = None
        self._input_worker: _InlineWorker | None = None

    @property
    def first_mutation_full_scans(self) -> int:
        return self._first_mutation_full_scans or 0

    def reset(self, phase: str, *, active: bool = False) -> None:
        """Reset visible state while preserving monotonic benchmark counters."""
        if phase not in self._PHASES:
            raise ValueError(f"unknown transaction fixture phase: {phase}")
        self.phase = phase
        self._active = active
        self._focused = None
        self._elements.clear()
        self._first_mutation_full_scans = None
        self._full_observations_at_reset = self.counters.full_observations
        self._inventory_calls_at_reset = self.counters.inventory_calls
        if self._active_subscriptions:
            raise RuntimeError("cannot reset with live fixture subscriptions")

        if self._saved is not None:
            from agent_eyes import server

            resolver = server._transaction_target_resolver
            if resolver is not None:
                resolver.invalidate()
            assert self._coordinator is not None
            self._coordinator.observations.invalidate_provider(
                provider="native",
                mode=OperationMode.FOREGROUND,
            )

    def transition(self, phase: str) -> None:
        if phase not in self._PHASES:
            raise ValueError(f"unknown transaction fixture phase: {phase}")
        self.phase = phase
        self.counters.revisions += 1

    def _before_dispatch(self) -> None:
        if self._first_mutation_full_scans is None:
            self._first_mutation_full_scans = (
                self.counters.full_observations
                - self._full_observations_at_reset
                + self.counters.inventory_calls
                - self._inventory_calls_at_reset
            )
        self.counters.dispatches += 1

    def _element(
        self,
        local_id: int,
        role: str,
        name: str,
        *,
        actions: list[str] | None = None,
        bounds: tuple[int, int, int, int] | None = None,
        value: str = "",
        children: list[UIElement] | None = None,
    ) -> UIElement:
        element = UIElement(
            id=local_id,
            role=role,
            name=name,
            value=value,
            actions=actions or [],
            bounds=bounds,
            children=children or [],
            platform_ref=object(),
            pid=FIXTURE_PID,
        )
        self._elements[local_id] = element
        return element

    def _tree(self) -> UIElement:
        self._elements = {}
        rows = [
            self._element(20, "group", "Unrelated virtualized diff row 1"),
            self._element(21, "group", "Unrelated virtualized diff row 2"),
        ]
        if self.phase != "virtualized":
            row_children: list[UIElement] = []
            if self.phase in {"control_visible", "editor_pending"}:
                row_children.append(
                    self._element(
                        31,
                        "button",
                        "Add inline comment",
                        actions=["press"],
                        bounds=(960, 360, 120, 30),
                    )
                )
            if self.phase in {"editor_visible", "typed"}:
                row_children.extend(
                    [
                        self._element(
                            32,
                            "textbox",
                            "Comment editor",
                            actions=["scrolltovisible"],
                            bounds=(640, 390, 420, 100),
                            value=FIXTURE_SECRET if self.phase == "typed" else "",
                        ),
                        self._element(
                            33,
                            "button",
                            "Save inline comment",
                            actions=["press"],
                            bounds=(980, 500, 80, 30),
                        ),
                    ]
                )
            if self.phase == "posted":
                row_children.append(
                    self._element(34, "article", "Posted inline comment")
                )
            rows.append(
                self._element(
                    30,
                    "group",
                    "Target diff row",
                    bounds=(120, 320, 980, 240),
                    children=row_children,
                )
            )
        return self._element(
            10,
            "document",
            "Pull request 482 diff",
            children=[
                self._element(11, "list", "Virtualized diff rows", children=rows)
            ],
        )

    def inventory(self, _adapter, *, require_complete: bool = False):
        if require_complete:
            raise AssertionError(
                "fixture inventory unexpectedly requested strict absence"
            )
        self.counters.inventory_calls += 1
        return [self.target]

    async def activate(self, target, timeout: float = 0.75, *, budget=None) -> bool:
        if (
            target.identifier != self.target.identifier
            or timeout <= 0
            or budget is not None
        ):
            raise AssertionError("fixture activated a different browser target")
        self.counters.activations += 1
        self._active = True
        return True

    async def shadow_inventory(self):
        self.counters.shadow_probes += 1
        return [], "none"

    def get_tree(self, pid: int, max_depth: int = 10) -> UIElement:
        if pid != FIXTURE_PID or max_depth != 10:
            raise AssertionError("fixture full observation scope changed")
        self.counters.full_observations += 1
        return self._tree()

    def get_subtree(self, element: UIElement, max_depth: int = 10) -> UIElement:
        if element is not self._window or max_depth != 10:
            raise AssertionError("fixture scoped observation used the wrong window")
        self.counters.scoped_observations += 1
        return self._tree()

    def is_element_valid(self, element: UIElement) -> bool:
        return element.platform_ref is not None

    def is_element_selected(self, element: UIElement | None) -> bool:
        return self._active and element is self._tab

    def is_window_focused(self, element: UIElement | None) -> bool:
        return self._active and element is self._window

    def element_at_position(self, x: int, y: int) -> UIElement | None:
        for element in self._elements.values():
            if element.bounds is None:
                continue
            left, top, width, height = element.bounds
            if left <= x <= left + width and top <= y <= top + height:
                return element
        return None

    @staticmethod
    def is_same_element(first: UIElement, second: UIElement) -> bool:
        return (
            first.id == second.id
            and first.role == second.role
            and first.name == second.name
        )

    def perform_action(self, element: UIElement, action: str) -> bool:
        if action != "press":
            return False
        self._before_dispatch()
        if element.id == 31 and self.phase == "control_visible":
            self.transition("editor_pending")
            return True
        if element.id == 33 and self.phase == "typed":
            self.counters.external_writes += 1
            self.transition("posted")
            return True
        return False

    def focus_element(self, element: UIElement) -> bool:
        self._focused = element
        return True

    def get_focused_element(self) -> UIElement | None:
        return self._focused

    def list_apps(self):
        raise AssertionError("transaction fixture must use its browser inventory")

    def is_available(self) -> bool:
        return True

    def is_frontmost(self, pid: int) -> bool:
        return self._active and pid == FIXTURE_PID

    def scroll(
        self,
        x: int,
        y: int,
        delta_x: int = 0,
        delta_y: int = -3,
    ) -> bool:
        if (x, y) != (400, 400) or not delta_y or delta_x:
            return False
        self._before_dispatch()
        self.transition("row_visible")
        return True

    def move_mouse(self, x: int, y: int) -> bool:
        hit = self.element_at_position(x, y)
        if hit is None or hit.id != 30 or self.phase != "row_visible":
            return False
        self._before_dispatch()
        self.transition("control_visible")
        return True

    def type_text(self, text: str) -> bool:
        if text != FIXTURE_SECRET or self.phase != "editor_visible":
            return False
        self._before_dispatch()
        self.transition("typed")
        return True

    def clear_and_type(self, text: str) -> bool:
        return self.type_text(text)

    async def subscription_factory(self, pid: int, _timeout: float = 0.0):
        if pid != FIXTURE_PID:
            return None
        self.counters.event_registrations += 1
        self._active_subscriptions += 1
        return _FixtureSubscription(self)

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        from agent_eyes import server

        self.counters.mcp_calls += 1
        return await server.call_tool(name, arguments)

    def resources(self) -> dict[str, int]:
        """Expose every retained fixture/runtime collection used by later stress."""
        if self._saved is None or self._coordinator is None:
            return {
                "active_subscriptions": self._active_subscriptions,
                "snapshots": 0,
                "observation_flights": 0,
                "shadow_locks": 0,
                "resolver_cache_entries": 0,
                "resolver_exact_leases": 0,
                "resolver_flights": 0,
                "active_workers": 0,
            }
        from agent_eyes import server

        resolver = server._transaction_target_resolver
        return {
            "active_subscriptions": self._active_subscriptions,
            "snapshots": len(self._coordinator.observations._snapshots),
            "observation_flights": len(self._coordinator._flights),
            "shadow_locks": len(self._coordinator._shadow_locks),
            "resolver_cache_entries": len(resolver._cache)
            if resolver is not None
            else 0,
            "resolver_exact_leases": (
                sum(len(bucket) for bucket in resolver._exact_leases.values())
                if resolver is not None
                else 0
            ),
            "resolver_flights": len(resolver._flights) if resolver is not None else 0,
            "active_workers": int(
                bool(
                    (self._native_worker and self._native_worker.active)
                    or (self._input_worker and self._input_worker.active)
                )
            ),
        }

    async def __aenter__(self) -> TransactionFixture:  # noqa: PYI034
        from agent_eyes import server

        if self._saved is not None:
            raise RuntimeError("transaction fixture is already installed")
        self._saved = {
            "collect_browser_targets": server.collect_browser_targets,
            "activate": server._activate_browser_target_and_wait,
            "shadow_inventory": server._collect_explicit_shadow_tabs,
            "native_adapter": server.native_adapter,
            "native_worker": server.native_worker,
            "input_worker": server.input_worker,
            "input_backend": server._input_backend,
            "runtime_readiness": server._runtime_readiness,
            "resolver": server._transaction_target_resolver,
            "coordinator": server.coordinator,
            "native_events": server.run_native_action_until,
            "telemetry": server._transaction_telemetry,
            "dispatch": server._DISPATCH_TABLE,
        }
        self._coordinator = AutomationCoordinator()
        self._native_worker = _InlineWorker(self.counters)
        self._input_worker = _InlineWorker(self.counters)

        async def deterministic_events(*args, **kwargs):
            kwargs["subscription_factory"] = self.subscription_factory
            return await run_native_action_until(*args, **kwargs)

        server.collect_browser_targets = self.inventory
        server._activate_browser_target_and_wait = self.activate
        server._collect_explicit_shadow_tabs = self.shadow_inventory
        server.native_adapter = self
        server.native_worker = self._native_worker
        server.input_worker = self._input_worker
        server._input_backend = self
        server._runtime_readiness = SimpleNamespace(core_ready=True)
        server._transaction_target_resolver = None
        server.coordinator = self._coordinator
        server.run_native_action_until = deterministic_events
        server._transaction_telemetry = None
        server._DISPATCH_TABLE = None
        return self

    async def __aexit__(self, *_exc_info) -> None:
        from agent_eyes import server

        saved = self._saved
        if saved is None:
            return
        try:
            assert self._coordinator is not None
            await self._coordinator.close()
            assert self._native_worker is not None
            assert self._input_worker is not None
            await asyncio.gather(
                self._native_worker.aclose(),
                self._input_worker.aclose(),
            )
            if self._active_subscriptions:
                raise RuntimeError("fixture leaked native subscriptions")
        finally:
            server.collect_browser_targets = saved["collect_browser_targets"]
            server._activate_browser_target_and_wait = saved["activate"]
            server._collect_explicit_shadow_tabs = saved["shadow_inventory"]
            server.native_adapter = saved["native_adapter"]
            server.native_worker = saved["native_worker"]
            server.input_worker = saved["input_worker"]
            server._input_backend = saved["input_backend"]
            server._runtime_readiness = saved["runtime_readiness"]
            server._transaction_target_resolver = saved["resolver"]
            server.coordinator = saved["coordinator"]
            server.run_native_action_until = saved["native_events"]
            server._transaction_telemetry = saved["telemetry"]
            server._DISPATCH_TABLE = saved["dispatch"]
            self._saved = None


def result_text(result: Any) -> str:
    content = result.content if isinstance(result, CallToolResult) else result
    return "\n".join(item.text for item in content)


def result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, CallToolResult) and result.isError:
        raise RuntimeError(f"fixture MCP call failed: {result_text(result)}")
    payload = json.loads(result_text(result))
    if not isinstance(payload, dict):
        raise TypeError("fixture MCP call did not return an object")
    return payload


def reveal_arguments() -> dict[str, Any]:
    return {
        "target": {"query": FIXTURE_QUERY},
        "steps": [
            {
                "op": "scroll",
                "delta_y": 400,
                "expect": {"role": "group", "name": "Target diff row"},
            },
            {
                "op": "locate",
                "as": "target_row",
                "role": "group",
                "name": "Target diff row",
            },
            {
                "op": "hover",
                "ref": "target_row",
                "expect": {"role": "button", "name": "Add inline comment"},
            },
        ],
        "expect": {"role": "button", "name": "Add inline comment"},
        "deadline_ms": 3_000,
    }


def post_arguments() -> dict[str, Any]:
    return {
        "target": {"query": FIXTURE_QUERY},
        "steps": [
            {
                "op": "locate",
                "as": "comment_button",
                "role": "button",
                "name": "Add inline comment",
            },
            {
                "op": "click",
                "ref": "comment_button",
                "expect": {"role": "textbox", "name": "Comment editor"},
            },
            {
                "op": "locate",
                "as": "editor",
                "role": "textbox",
                "name": "Comment editor",
            },
            {"op": "type", "ref": "editor", "text": FIXTURE_SECRET},
            {
                "op": "locate",
                "as": "submit",
                "role": "button",
                "name": "Save inline comment",
            },
            {
                "op": "click",
                "ref": "submit",
                "consequence": "external_write",
            },
        ],
        "expect": {"role": "article", "name": "Posted inline comment"},
        "deadline_ms": 3_000,
    }


def observe_arguments() -> dict[str, Any]:
    return {
        "query": FIXTURE_QUERY,
        "intent": "inspect",
        "selectors": [{"role": "button", "name": "Add inline comment"}],
        "max_results": 1,
        "deadline_ms": 3_000,
    }
