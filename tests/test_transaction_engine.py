from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from agent_eyes.action_kernel import ActionDispatchResult, ActionPorts
from agent_eyes.adapters.base import UIElement
from agent_eyes.locators import LocatorIndex
from agent_eyes.operation import OperationBudget, OperationErrorCode
from agent_eyes.transaction_contract import (
    ExecuteRequest,
    Locator,
    TargetMode,
    TargetSpec,
    TransactionOperation,
    TransactionStep,
)
from agent_eyes.transactions import (
    TransactionEngine,
    TransactionPorts,
    TransactionResult,
    TransactionStatus,
    TransactionTarget,
    TransactionView,
)


def _run(coroutine):
    return asyncio.run(coroutine)


def _request(
    steps: tuple[TransactionStep, ...],
    *,
    final_expect: Locator | None = None,
    mode: TargetMode = TargetMode.FOREGROUND,
    deadline_ms: int = 3_000,
) -> ExecuteRequest:
    return ExecuteRequest(
        target=TargetSpec(mode=mode, pid=73)
        if mode is TargetMode.FOREGROUND
        else TargetSpec(mode=mode, target_id="cdp:test"),
        steps=steps,
        final_expect=final_expect,
        deadline_ms=deadline_ms,
        consequential_step=None,
    )


def _view(*elements: UIElement, snapshot: str) -> TransactionView:
    return TransactionView(
        index=LocatorIndex.from_roots(elements),
        snapshot=snapshot,
    )


def _success_ports(
    *,
    initial: TransactionView,
    refreshed: list[TransactionView] | None = None,
    events: list[str] | None = None,
) -> TransactionPorts:
    event_log = events if events is not None else []
    refreshes = list(refreshed or [])

    async def resolve(spec, activate, _budget):
        event_log.append(f"resolve:{activate}")
        return TransactionTarget(target_id=f"pid:{spec.pid}")

    async def observe(_target, _budget):
        event_log.append("observe")
        return initial

    async def refresh(_target, _current, _locator, _budget):
        event_log.append("refresh")
        if not refreshes:
            raise AssertionError("unexpected refresh")
        return refreshes.pop(0)

    def action_ports(step, _element, _target):
        async def capability(_budget):
            event_log.append(f"capability:{step.operation.value}")
            return True

        async def dispatch(_budget):
            event_log.append(f"dispatch:{step.operation.value}")
            return ActionDispatchResult.succeeded(changed=True)

        return ActionPorts(
            provider_code="native.test",
            capability=capability,
            dispatch=dispatch,
        )

    return TransactionPorts(
        resolve=resolve,
        observe=observe,
        refresh=refresh,
        action_ports=action_ports,
    )


def test_bitbucket_like_six_step_journey_resolves_and_observes_once() -> None:
    async def run() -> None:
        secret = "private inline review comment 72f1e8"
        comment = UIElement(id=1, role="button", name="Add inline comment")
        editor = UIElement(id=2, role="textbox", name="Comment editor")
        save = UIElement(id=3, role="button", name="Save")
        posted = UIElement(id=4, role="article", name="Posted comment")
        events: list[str] = []
        typed_payloads = 0

        initial = _view(comment, snapshot="n0")
        after_open = _view(editor, snapshot="n1")
        after_type = _view(editor, save, snapshot="n2")
        after_submit = _view(posted, snapshot="n3")
        refreshes = [after_open, after_type, after_submit]

        async def resolve(spec, activate, _budget):
            events.append(f"resolve:{activate}")
            assert spec.pid == 73
            return TransactionTarget(target_id="pid:73")

        async def observe(_target, _budget):
            events.append("observe")
            return initial

        async def refresh(_target, _current, _locator, _budget):
            events.append("refresh")
            return refreshes.pop(0)

        def action_ports(step, element, _target):
            async def capability(_budget):
                return True

            async def dispatch(_budget):
                nonlocal typed_payloads
                events.append(f"dispatch:{step.operation.value}:{element.id}")
                if step.operation is TransactionOperation.TYPE:
                    assert step.text == secret
                    typed_payloads += 1
                return ActionDispatchResult.succeeded(changed=True)

            return ActionPorts(
                provider_code="native.test",
                capability=capability,
                dispatch=dispatch,
            )

        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="comment_button",
                    locator=Locator(role="button", name="Add inline comment"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.CLICK,
                    ref="comment_button",
                    expect=Locator(role="textbox"),
                ),
                TransactionStep(
                    index=2,
                    operation=TransactionOperation.LOCATE,
                    alias="editor",
                    locator=Locator(role="textbox"),
                ),
                TransactionStep(
                    index=3,
                    operation=TransactionOperation.TYPE,
                    ref="editor",
                    text=secret,
                ),
                TransactionStep(
                    index=4,
                    operation=TransactionOperation.LOCATE,
                    alias="submit",
                    locator=Locator(role="button", name="Save"),
                ),
                TransactionStep(
                    index=5,
                    operation=TransactionOperation.CLICK,
                    ref="submit",
                    consequence="external_write",
                ),
            ),
            final_expect=Locator(role="article", name="Posted comment"),
        )

        result = await TransactionEngine().run(
            request,
            ports=TransactionPorts(
                resolve=resolve,
                observe=observe,
                refresh=refresh,
                action_ports=action_ports,
            ),
        )

        assert result.status is TransactionStatus.SUCCEEDED
        assert result.code is None
        assert result.completed_steps == 6
        assert result.failed_step is None
        assert result.final_expectation is True
        assert result.snapshot == "n3"
        assert typed_payloads == 1
        assert events.count("resolve:True") == 1
        assert events.count("observe") == 1
        assert events.count("refresh") == 3
        assert [event for event in events if event.startswith("dispatch:")] == [
            "dispatch:click:1",
            "dispatch:type:2",
            "dispatch:click:3",
        ]
        assert secret not in repr(result)
        assert secret not in str(result)

    _run(run())


def test_shadow_resolution_is_explicit_and_never_requests_activation() -> None:
    async def run() -> None:
        requested: list[bool] = []
        element = UIElement(id=1, role="button", name="Inspect")
        initial = _view(element, snapshot="s0")
        ports = _success_ports(initial=initial)

        async def resolve(_spec, activate, _budget):
            requested.append(activate)
            return TransactionTarget(target_id="cdp:test")

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.EXPECT,
                        locator=Locator(role="button"),
                    ),
                ),
                mode=TargetMode.SHADOW,
            ),
            ports=TransactionPorts(
                resolve=resolve,
                observe=ports.observe,
                refresh=ports.refresh,
                action_ports=ports.action_ports,
            ),
        )

        assert result.status is TransactionStatus.SUCCEEDED
        assert requested == [False]

    _run(run())


def test_first_step_unscoped_scroll_observes_before_dispatch() -> None:
    async def run() -> None:
        events: list[str] = []
        after = UIElement(id=4, role="button", name="Revealed")
        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.SCROLL,
                    delta_y=300,
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.LOCATE,
                    alias="after",
                    locator=Locator(role="button", name="Revealed"),
                ),
            )
        )
        result = await TransactionEngine().run(
            request,
            ports=_success_ports(
                initial=_view(after, snapshot="n0"),
                refreshed=[_view(after, snapshot="n1")],
                events=events,
            ),
        )

        assert result.status is TransactionStatus.SUCCEEDED
        assert events.index("observe") < events.index("dispatch:scroll")

    _run(run())


def test_read_only_steps_share_one_immutable_observation_without_refresh() -> None:
    async def run() -> None:
        group = UIElement(
            id=1,
            role="group",
            name="Discussion",
            children=[UIElement(id=2, role="button", name="Save")],
        )
        events: list[str] = []
        ports = _success_ports(initial=_view(group, snapshot="n0"), events=events)
        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="discussion",
                    locator=Locator(role="group"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.LOCATE,
                    alias="save",
                    locator=Locator(role="button", within="discussion"),
                ),
                TransactionStep(
                    index=2,
                    operation=TransactionOperation.EXPECT,
                    locator=Locator(role="button", within="discussion"),
                ),
            )
        )

        result = await TransactionEngine().run(request, ports=ports)

        assert result.status is TransactionStatus.SUCCEEDED
        assert events == ["resolve:True", "observe"]

    _run(run())


def test_refresh_occurs_only_when_a_dirty_view_is_needed() -> None:
    async def run() -> None:
        first = UIElement(id=1, role="button", name="First")
        second = UIElement(id=2, role="button", name="Second")
        events: list[str] = []
        ports = _success_ports(
            initial=_view(first, snapshot="n0"),
            refreshed=[_view(second, snapshot="n1")],
            events=events,
        )
        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="first",
                    locator=Locator(name="First"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.CLICK,
                    ref="first",
                ),
                TransactionStep(
                    index=2,
                    operation=TransactionOperation.LOCATE,
                    alias="second",
                    locator=Locator(name="Second"),
                ),
                TransactionStep(
                    index=3,
                    operation=TransactionOperation.CLICK,
                    ref="second",
                ),
            )
        )

        result = await TransactionEngine().run(request, ports=ports)

        assert result.status is TransactionStatus.SUCCEEDED
        assert events.count("refresh") == 1
        assert events.count("dispatch:click") == 2
        assert events.index("refresh") > events.index("dispatch:click")

    _run(run())


def test_refresh_invalidates_revision_scoped_aliases() -> None:
    async def run() -> None:
        button = UIElement(id=1, role="button", name="Open")
        actions = 0
        events: list[str] = []
        base = _success_ports(initial=_view(button, snapshot="n0"), events=events)

        def action_ports(step, element, target):
            nonlocal actions
            actions += 1
            return base.action_ports(step, element, target)

        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="open",
                    locator=Locator(role="button"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.CLICK,
                    ref="open",
                ),
                TransactionStep(
                    index=2,
                    operation=TransactionOperation.CLICK,
                    ref="open",
                ),
            )
        )

        result = await TransactionEngine().run(
            request,
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=base.observe,
                refresh=base.refresh,
                action_ports=action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.ELEMENT_NOT_FOUND
        assert result.failed_step == 3
        assert result.completed_steps == 2
        assert result.retry_safe is False
        assert result.snapshot == ""
        assert actions == 1
        assert events.count("refresh") == 0

    _run(run())


def test_post_action_expect_re_resolves_a_within_scope_in_the_new_revision() -> None:
    async def run() -> None:
        initial_group = UIElement(
            id=1,
            role="group",
            name="Discussion",
            children=[UIElement(id=2, role="button", name="Reply")],
        )
        refreshed_group = UIElement(
            id=3,
            role="group",
            name="Discussion",
            children=[UIElement(id=4, role="textbox", name="Reply editor")],
        )
        events: list[str] = []
        ports = _success_ports(
            initial=_view(initial_group, snapshot="n0"),
            refreshed=[_view(refreshed_group, snapshot="n1")],
            events=events,
        )
        request = _request(
            (
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="discussion",
                    locator=Locator(role="group", name="Discussion"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.LOCATE,
                    alias="reply",
                    locator=Locator(
                        role="button",
                        name="Reply",
                        within="discussion",
                    ),
                ),
                TransactionStep(
                    index=2,
                    operation=TransactionOperation.CLICK,
                    ref="reply",
                    expect=Locator(
                        role="textbox",
                        name="Reply editor",
                        within="discussion",
                    ),
                ),
            )
        )

        result = await TransactionEngine().run(request, ports=ports)

        assert result.status is TransactionStatus.SUCCEEDED
        assert result.completed_steps == 3
        assert events.count("refresh") == 1

    _run(run())


def test_ambiguous_locator_stops_before_actions_and_is_retryable() -> None:
    async def run() -> None:
        initial = _view(
            UIElement(id=1, role="button", name="Save"),
            UIElement(id=2, role="button", name="Save"),
            snapshot="n0",
        )
        actions = 0
        base = _success_ports(initial=initial)

        def action_ports(*_args):
            nonlocal actions
            actions += 1
            raise AssertionError("action must not be prepared")

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.LOCATE,
                        alias="save",
                        locator=Locator(role="button"),
                    ),
                    TransactionStep(
                        index=1,
                        operation=TransactionOperation.CLICK,
                        ref="save",
                    ),
                )
            ),
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=base.observe,
                refresh=base.refresh,
                action_ports=action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.AMBIGUOUS_ELEMENT
        assert result.failed_step == 1
        assert result.completed_steps == 0
        assert result.retry_safe is True
        assert actions == 0

    _run(run())


def test_transaction_with_external_write_is_never_marked_retryable() -> None:
    async def run() -> None:
        async def resolve(_spec, _activate, _budget):
            raise RuntimeError("provider unavailable before dispatch")

        request = ExecuteRequest(
            target=TargetSpec(mode=TargetMode.FOREGROUND, pid=73),
            steps=(
                TransactionStep(
                    index=0,
                    operation=TransactionOperation.LOCATE,
                    alias="save",
                    locator=Locator(role="button", name="Save"),
                ),
                TransactionStep(
                    index=1,
                    operation=TransactionOperation.CLICK,
                    ref="save",
                    consequence="external_write",
                ),
            ),
            final_expect=None,
            deadline_ms=3_000,
            consequential_step=1,
        )
        ports = TransactionPorts(
            resolve=resolve,
            observe=lambda *_args: pytest.fail("observe must not run"),
            refresh=lambda *_args: pytest.fail("refresh must not run"),
            action_ports=lambda *_args: pytest.fail("action must not run"),
        )

        result = await TransactionEngine().run(request, ports=ports)

        assert result.status is TransactionStatus.FAILED
        assert result.retry_safe is False
        assert result.completed_steps == 0

    _run(run())


def test_failed_action_stops_later_steps_and_preserves_kernel_retry_safety() -> None:
    async def run() -> None:
        button = UIElement(id=1, role="button", name="Save")
        later = 0
        base = _success_ports(initial=_view(button, snapshot="n0"))

        def action_ports(step, _element, _target):
            async def capability(_budget):
                return True

            async def dispatch(_budget):
                nonlocal later
                if step.index > 1:
                    later += 1
                return ActionDispatchResult.failed(
                    OperationErrorCode.UNSUPPORTED_CAPABILITY,
                    retry_safe=True,
                )

            return ActionPorts(
                provider_code="native.test",
                capability=capability,
                dispatch=dispatch,
            )

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.LOCATE,
                        alias="save",
                        locator=Locator(role="button"),
                    ),
                    TransactionStep(
                        index=1,
                        operation=TransactionOperation.CLICK,
                        ref="save",
                    ),
                    TransactionStep(
                        index=2,
                        operation=TransactionOperation.PRESS_KEY,
                        ref="save",
                        key="Enter",
                    ),
                )
            ),
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=base.observe,
                refresh=base.refresh,
                action_ports=action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert result.failed_step == 2
        assert result.completed_steps == 1
        assert result.retry_safe is True
        assert result.snapshot == ""
        assert later == 0

    _run(run())


def test_outcome_unknown_stops_immediately_and_is_never_retryable() -> None:
    async def run() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        later = 0
        button = UIElement(id=1, role="button", name="Save")
        base = _success_ports(initial=_view(button, snapshot="n0"))

        def action_ports(step, _element, _target):
            async def capability(_budget):
                return True

            async def dispatch(_budget):
                nonlocal later
                if step.index > 1:
                    later += 1
                    return ActionDispatchResult.succeeded(changed=True)
                started.set()
                await blocker.wait()
                return ActionDispatchResult.succeeded(changed=True)

            return ActionPorts(
                provider_code="native.test",
                capability=capability,
                dispatch=dispatch,
            )

        task = asyncio.create_task(
            TransactionEngine().run(
                _request(
                    (
                        TransactionStep(
                            index=0,
                            operation=TransactionOperation.LOCATE,
                            alias="save",
                            locator=Locator(role="button"),
                        ),
                        TransactionStep(
                            index=1,
                            operation=TransactionOperation.CLICK,
                            ref="save",
                        ),
                        TransactionStep(
                            index=2,
                            operation=TransactionOperation.PRESS_KEY,
                            ref="save",
                            key="Enter",
                        ),
                    )
                ),
                ports=TransactionPorts(
                    resolve=base.resolve,
                    observe=base.observe,
                    refresh=base.refresh,
                    action_ports=action_ports,
                ),
            )
        )
        await started.wait()
        task.cancel()
        result = await task

        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.failed_step == 2
        assert result.completed_steps == 1
        assert result.retry_safe is False
        assert result.snapshot == ""
        assert later == 0

    _run(run())


def test_cancellation_during_resolution_propagates_before_observation() -> None:
    async def run() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        observed = 0

        async def resolve(_spec, _activate, _budget):
            started.set()
            await blocker.wait()
            return TransactionTarget(target_id="pid:73")

        async def observe(_target, _budget):
            nonlocal observed
            observed += 1
            return _view(UIElement(id=1, role="button"), snapshot="n0")

        ports = TransactionPorts(
            resolve=resolve,
            observe=observe,
            refresh=lambda *_args: pytest.fail("refresh must not run"),
            action_ports=lambda *_args: pytest.fail("action must not run"),
        )
        task = asyncio.create_task(
            TransactionEngine().run(
                _request(
                    (
                        TransactionStep(
                            index=0,
                            operation=TransactionOperation.EXPECT,
                            locator=Locator(role="button"),
                        ),
                    )
                ),
                ports=ports,
            )
        )
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert observed == 0

    _run(run())


def test_cancellation_after_replay_unsafe_target_returns_outcome_unknown() -> None:
    async def run() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def resolve(_spec, _activate, _budget):
            return TransactionTarget(target_id="pid:73", replay_unsafe=True)

        async def observe(_target, _budget):
            started.set()
            await blocker.wait()
            return _view(UIElement(id=1, role="button"), snapshot="unused")

        ports = TransactionPorts(
            resolve=resolve,
            observe=observe,
            refresh=lambda *_args: pytest.fail("refresh must not run"),
            action_ports=lambda *_args: pytest.fail("action must not run"),
        )
        task = asyncio.create_task(
            TransactionEngine().run(
                _request(
                    (
                        TransactionStep(
                            index=0,
                            operation=TransactionOperation.EXPECT,
                            locator=Locator(role="button"),
                        ),
                    )
                ),
                ports=ports,
            )
        )
        await started.wait()
        task.cancel()

        result = await task
        assert result.status is TransactionStatus.OUTCOME_UNKNOWN
        assert result.code is OperationErrorCode.OUTCOME_UNKNOWN
        assert result.retry_safe is False

    _run(run())


def test_deadline_during_initial_observation_returns_safe_failure() -> None:
    async def run() -> None:
        blocker = asyncio.Event()
        base = _success_ports(
            initial=_view(UIElement(id=1, role="button"), snapshot="unused")
        )

        async def observe(_target, _budget):
            await blocker.wait()
            return _view(UIElement(id=1, role="button"), snapshot="unused")

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.EXPECT,
                        locator=Locator(role="button"),
                    ),
                ),
                deadline_ms=5,
            ),
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=observe,
                refresh=base.refresh,
                action_ports=base.action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert result.failed_step == 1
        assert result.completed_steps == 0
        assert result.retry_safe is True

    _run(run())


def test_deadline_during_post_mutation_refresh_is_not_retryable() -> None:
    async def run() -> None:
        blocker = asyncio.Event()
        button = UIElement(id=1, role="button", name="Save")
        base = _success_ports(initial=_view(button, snapshot="n0"))

        async def refresh(_target, _current, _locator, _budget):
            await blocker.wait()
            return _view(UIElement(id=2, role="status"), snapshot="n1")

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.LOCATE,
                        alias="save",
                        locator=Locator(role="button"),
                    ),
                    TransactionStep(
                        index=1,
                        operation=TransactionOperation.CLICK,
                        ref="save",
                    ),
                ),
                final_expect=Locator(role="status"),
                deadline_ms=10,
            ),
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=base.observe,
                refresh=refresh,
                action_ports=base.action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert result.completed_steps == 2
        assert result.failed_step is None
        assert result.final_expectation is False
        assert result.retry_safe is False
        assert result.snapshot == ""

    _run(run())


def test_expired_parent_budget_prevents_resolution() -> None:
    async def run() -> None:
        resolved = 0
        initial = _view(UIElement(id=1, role="button"), snapshot="n0")
        base = _success_ports(initial=initial)

        async def resolve(_spec, _activate, _budget):
            nonlocal resolved
            resolved += 1
            return TransactionTarget(target_id="pid:73")

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.EXPECT,
                        locator=Locator(role="button"),
                    ),
                )
            ),
            ports=TransactionPorts(
                resolve=resolve,
                observe=base.observe,
                refresh=base.refresh,
                action_ports=base.action_ports,
            ),
            budget=OperationBudget.start(0.0),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.DEADLINE_EXCEEDED
        assert result.completed_steps == 0
        assert result.retry_safe is True
        assert resolved == 0

    _run(run())


def test_final_expectation_failure_keeps_completed_steps_and_latest_snapshot() -> None:
    async def run() -> None:
        button = UIElement(id=1, role="button", name="Save")
        base = _success_ports(
            initial=_view(button, snapshot="n0"),
            refreshed=[
                _view(UIElement(id=2, role="status", name="Pending"), snapshot="n1")
            ],
        )
        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.LOCATE,
                        alias="save",
                        locator=Locator(role="button"),
                    ),
                    TransactionStep(
                        index=1,
                        operation=TransactionOperation.CLICK,
                        ref="save",
                    ),
                ),
                final_expect=Locator(role="article", name="Posted"),
            ),
            ports=base,
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.ELEMENT_NOT_FOUND
        assert result.completed_steps == 2
        assert result.failed_step is None
        assert result.final_expectation is False
        assert result.retry_safe is False
        assert result.snapshot == "n1"

    _run(run())


def test_action_factory_exception_cannot_leak_typed_text() -> None:
    async def run() -> None:
        secret = "private comment factory failure f4e9"
        editor = UIElement(id=1, role="textbox")
        base = _success_ports(initial=_view(editor, snapshot="n0"))

        def action_ports(_step, _element, _target):
            raise RuntimeError(secret)

        result = await TransactionEngine().run(
            _request(
                (
                    TransactionStep(
                        index=0,
                        operation=TransactionOperation.LOCATE,
                        alias="editor",
                        locator=Locator(role="textbox"),
                    ),
                    TransactionStep(
                        index=1,
                        operation=TransactionOperation.TYPE,
                        ref="editor",
                        text=secret,
                    ),
                )
            ),
            ports=TransactionPorts(
                resolve=base.resolve,
                observe=base.observe,
                refresh=base.refresh,
                action_ports=action_ports,
            ),
        )

        assert result.status is TransactionStatus.FAILED
        assert result.code is OperationErrorCode.UNSUPPORTED_CAPABILITY
        assert secret not in repr(result)
        assert secret not in str(result)

    _run(run())


def test_transaction_result_and_view_are_frozen() -> None:
    view = _view(UIElement(id=1, role="button"), snapshot="n0")
    result = TransactionResult(
        status=TransactionStatus.SUCCEEDED,
        code=None,
        target_id="pid:73",
        completed_steps=1,
        failed_step=None,
        elapsed_ms=1,
        retry_safe=False,
        final_expectation=None,
        snapshot="n0",
    )

    with pytest.raises(FrozenInstanceError):
        view.snapshot = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.completed_steps = 0  # type: ignore[misc]
