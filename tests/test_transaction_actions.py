from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_eyes.action_kernel import ActionKernel, ActionOutcomeStatus
from agent_eyes.adapters.base import UIElement
from agent_eyes.operation import OperationBudget, OperationErrorCode
from agent_eyes.target_resolver import ResolvedTarget, ResolutionSource
from agent_eyes.transaction_contract import (
    TargetMode,
    TransactionOperation,
    TransactionStep,
)


def _target(pid: int = 73) -> ResolvedTarget:
    return ResolvedTarget(
        mode=TargetMode.FOREGROUND,
        target_id=f"pid:{pid}",
        pid=pid,
        source=ResolutionSource.PID,
    )


def test_transaction_click_selects_one_native_action_without_fallback(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.perform_action.return_value = False
    input_backend = MagicMock()
    input_backend.is_frontmost.return_value = True
    invalidated = MagicMock()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", invalidated)
    element = UIElement(
        id=4,
        role="button",
        name="Save",
        actions=["press", "click"],
        pid=73,
    )
    step = TransactionStep(index=0, operation=TransactionOperation.CLICK, ref="save")

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
    assert outcome.error_code is OperationErrorCode.OUTCOME_UNKNOWN
    adapter.perform_action.assert_called_once_with(element, "press")
    input_backend.click.assert_not_called()
    invalidated.assert_called_once_with(pid=73, target_id="pid:73")


def test_transaction_coordinate_click_dispatches_once_when_no_ax_action(monkeypatch):
    from agent_eyes import server

    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    input_backend.click.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())
    element = UIElement(
        id=4,
        role="button",
        name="Save",
        bounds=(10, 20, 40, 20),
        pid=73,
    )
    adapter.element_at_position.return_value = element
    adapter.is_same_element.return_value = True
    step = TransactionStep(index=0, operation=TransactionOperation.CLICK, ref="save")

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.SUCCEEDED
    input_backend.click.assert_called_once_with(30, 30)
    adapter.perform_action.assert_not_called()


def test_transaction_coordinate_click_rejects_changed_hit_target_before_dispatch(
    monkeypatch,
):
    from agent_eyes import server

    element = UIElement(
        id=4,
        role="button",
        name="Save",
        bounds=(10, 20, 40, 20),
        pid=73,
    )
    overlay = UIElement(id=8, role="button", name="Delete", pid=73)
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.element_at_position.return_value = overlay
    adapter.is_same_element.return_value = False
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())
    step = TransactionStep(index=0, operation=TransactionOperation.CLICK, ref="save")

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.FAILED
    assert outcome.error_code is OperationErrorCode.FOCUS_MISMATCH
    assert outcome.retry_safe is True
    adapter.element_at_position.assert_called_once_with(30, 30)
    input_backend.click.assert_not_called()


def test_transaction_type_focuses_exact_element_then_bulk_dispatches_once(monkeypatch):
    from agent_eyes import server

    typed_text = "private review text 24c5"
    element = UIElement(
        id=7,
        role="textbox",
        name="Comment",
        actions=["scrolltovisible"],
        pid=73,
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.focus_element.return_value = True
    adapter.get_focused_element.side_effect = [None, element]
    adapter.is_same_element.side_effect = lambda first, second: first is second
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    input_backend.type_text.return_value = True
    invalidated = MagicMock()
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", invalidated)
    step = TransactionStep(
        index=0,
        operation=TransactionOperation.TYPE,
        ref="editor",
        text=typed_text,
    )

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.SUCCEEDED
    adapter.focus_element.assert_called_once_with(element)
    input_backend.type_text.assert_called_once_with(typed_text)
    input_backend.clear_and_type.assert_not_called()
    invalidated.assert_called_once_with(pid=73, target_id="pid:73")
    assert typed_text not in repr(outcome)


def test_transaction_partial_clear_and_type_failure_is_never_retryable(monkeypatch):
    from agent_eyes import server

    element = UIElement(
        id=7,
        role="textbox",
        name="Comment",
        pid=73,
        platform_ref=object(),
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.get_focused_element.return_value = element
    adapter.is_same_element.return_value = True
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    input_backend.clear_and_type.return_value = False
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())
    step = TransactionStep(
        index=0,
        operation=TransactionOperation.TYPE,
        ref="editor",
        text="private review",
    )

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.OUTCOME_UNKNOWN
    assert outcome.error_code is OperationErrorCode.OUTCOME_UNKNOWN
    assert outcome.retry_safe is False
    input_backend.clear_and_type.assert_called_once_with("private review")


def test_transaction_scroll_dispatches_one_bounded_wheel_call(monkeypatch):
    from agent_eyes import server

    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    input_backend.scroll.return_value = True
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())
    step = TransactionStep(
        index=0,
        operation=TransactionOperation.SCROLL,
        delta_x=200,
        delta_y=300,
    )

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, None, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.SUCCEEDED
    input_backend.scroll.assert_called_once_with(400, 400, delta_x=-2, delta_y=-3)


@pytest.mark.parametrize(
    ("operation", "input_method"),
    [
        (TransactionOperation.HOVER, "move_mouse"),
        (TransactionOperation.SCROLL, "scroll"),
    ],
)
def test_transaction_pointer_actions_reject_changed_hit_target_before_dispatch(
    monkeypatch,
    operation,
    input_method,
):
    from agent_eyes import server

    element = UIElement(
        id=4,
        role="group",
        name="Review area",
        bounds=(10, 20, 40, 20),
        pid=73,
    )
    adapter = MagicMock()
    adapter.is_element_valid.return_value = True
    adapter.element_at_position.return_value = UIElement(
        id=8,
        role="button",
        name="Overlay",
    )
    adapter.is_same_element.return_value = False
    input_backend = MagicMock()
    input_backend.is_available.return_value = True
    input_backend.is_frontmost.return_value = True
    monkeypatch.setattr(server, "native_adapter", adapter)
    monkeypatch.setattr(server, "_input_backend", input_backend)
    monkeypatch.setattr(server, "_invalidate_native_mutation_state", MagicMock())
    step = TransactionStep(
        index=0,
        operation=operation,
        ref="area",
        delta_y=300 if operation is TransactionOperation.SCROLL else 0,
    )

    outcome = asyncio.run(
        ActionKernel().run(
            step.operation,
            ports=server._transaction_action_ports(step, element, _target()),
            budget=OperationBudget.start(1.0),
        )
    )

    assert outcome.status is ActionOutcomeStatus.FAILED
    assert outcome.error_code is OperationErrorCode.FOCUS_MISMATCH
    assert outcome.retry_safe is True
    getattr(input_backend, input_method).assert_not_called()
