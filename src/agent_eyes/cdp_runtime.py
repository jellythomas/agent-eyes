"""Fail-closed CDP Runtime action envelopes and AX identity checks."""

from __future__ import annotations

from enum import Enum


class RuntimeActionStatus(Enum):
    """Browser-owned statuses emitted only by fixed Agent Eyes wrappers."""

    CLICK_APPLIED = "__agent_eyes_click_applied_v1__"


class RuntimeActionOutcomeUnknown(RuntimeError):
    """A runtime call entered page code but did not return a trusted status."""


CLICK_FUNCTION = f"""function() {{
    this.click();
    return {RuntimeActionStatus.CLICK_APPLIED.value!r};
}}"""


def parse_runtime_action_status(
    envelope: object,
    *,
    allowed: frozenset[RuntimeActionStatus],
) -> RuntimeActionStatus:
    """Accept only an exception-free, fixed wrapper return value.

    Runtime exception text is page-controlled. It is therefore never used to
    classify an action as retry-safe, even when it contains a familiar marker.
    """
    if not isinstance(envelope, dict):
        raise RuntimeActionOutcomeUnknown("CDP returned an invalid Runtime envelope")
    if envelope.get("exceptionDetails") is not None:
        raise RuntimeActionOutcomeUnknown("CDP Runtime action raised an exception")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise RuntimeActionOutcomeUnknown("CDP Runtime action omitted its result")
    value = result.get("value")
    try:
        status = RuntimeActionStatus(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeActionOutcomeUnknown(
            "CDP Runtime action returned an untrusted status"
        ) from exc
    if status not in allowed:
        raise RuntimeActionOutcomeUnknown(
            "CDP Runtime action returned a status for another operation"
        )
    return status


def require_empty_command_result(envelope: object) -> None:
    """Require the exact result shape of a successful fieldless CDP command."""
    if envelope != {}:
        raise RuntimeActionOutcomeUnknown(
            "CDP command returned a malformed acknowledgement"
        )


def ax_element_semantics_match(
    envelope: object,
    *,
    backend_node_id: int,
    expected_role: str,
    expected_name: str,
) -> bool:
    """Match one exact, non-ignored AX node to its observed role and name."""
    try:
        node = _exact_ax_node(envelope, backend_node_id)
    except RuntimeActionOutcomeUnknown:
        return False
    if node is None:
        return False
    if node.get("ignored") is not False:
        return False
    role = _ax_string(node.get("role"))
    name = _ax_string(node.get("name"))
    return role == expected_role and name == expected_name


def ax_element_has_exact_focus(
    envelope: object,
    *,
    backend_node_id: int,
    expected_role: str,
    expected_name: str,
) -> bool:
    """Read exact focus from browser-owned AX state, never page JavaScript."""
    node = _exact_ax_node(envelope, backend_node_id)
    if node is None or node.get("ignored") is not False:
        return False
    if (
        _ax_string(node.get("role")) != expected_role
        or _ax_string(node.get("name")) != expected_name
    ):
        return False
    properties = node.get("properties", [])
    if not isinstance(properties, list) or any(
        not isinstance(prop, dict) for prop in properties
    ):
        raise RuntimeActionOutcomeUnknown("CDP returned malformed AX focus properties")
    focused = [prop for prop in properties if prop.get("name") == "focused"]
    if not focused:
        return False
    if len(focused) != 1:
        raise RuntimeActionOutcomeUnknown("CDP returned ambiguous AX focus state")
    value = focused[0].get("value")
    if not isinstance(value, dict) or not isinstance(value.get("value"), bool):
        raise RuntimeActionOutcomeUnknown("CDP returned malformed AX focus state")
    return value["value"] is True


def _exact_ax_node(envelope: object, backend_node_id: int) -> dict | None:
    if not isinstance(envelope, dict):
        raise RuntimeActionOutcomeUnknown("CDP returned an invalid AX envelope")
    nodes = envelope.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise RuntimeActionOutcomeUnknown("CDP returned malformed AX nodes")
    matches = [
        node for node in nodes if node.get("backendDOMNodeId") == backend_node_id
    ]
    return matches[0] if len(matches) == 1 else None


def _ax_string(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    inner = value.get("value")
    return inner if isinstance(inner, str) else None
