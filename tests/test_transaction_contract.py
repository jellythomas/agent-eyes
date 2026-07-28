from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from agent_eyes.input_validation import InputValidationError, validate_tool_arguments
from agent_eyes.operation import OperationErrorCode
from agent_eyes.transaction_contract import (
    DEFAULT_TRANSACTION_DEADLINE_MS,
    EXECUTE_INPUT_SCHEMA,
    MAX_TRANSACTION_SELECTORS,
    MAX_TRANSACTION_STEPS,
    OBSERVE_TARGET_INPUT_SCHEMA,
    MatchMode,
    TargetIntent,
    TargetMode,
    TransactionOperation,
    parse_execute_request,
    parse_observe_target_request,
)


def _execute_request() -> dict:
    return {
        "target": {
            "query": "pull request 42",
            "mode": "foreground",
        },
        "steps": [
            {
                "op": "locate",
                "as": "comment",
                "role": "button",
                "name": "comment",
                "match": "contains",
            },
            {
                "op": "click",
                "ref": "comment",
                "expect": {"role": "textbox", "match": "exact"},
            },
            {
                "op": "locate",
                "as": "editor",
                "role": "textbox",
            },
            {
                "op": "type",
                "ref": "editor",
                "text": "Looks good to me",
            },
            {
                "op": "locate",
                "as": "save",
                "role": "button",
                "name": "Save",
            },
            {
                "op": "click",
                "ref": "save",
                "consequence": "external_write",
            },
        ],
        "expect": {
            "role": "article",
            "name": "posted comment",
            "match": "contains",
        },
        "deadline_ms": 3000,
    }


def test_public_schemas_accept_known_and_discovery_examples():
    observe = {
        "query": "Bitbucket pull request 42",
        "intent": "interact",
        "selectors": [{"role": "button", "name": "comment", "match": "contains"}],
        "max_results": 10,
        "deadline_ms": 3000,
    }

    validate_tool_arguments(OBSERVE_TARGET_INPUT_SCHEMA, observe)
    validate_tool_arguments(EXECUTE_INPUT_SCHEMA, _execute_request())


def test_public_schemas_are_valid_draft_2020_12():
    Draft202012Validator.check_schema(OBSERVE_TARGET_INPUT_SCHEMA)
    Draft202012Validator.check_schema(EXECUTE_INPUT_SCHEMA)


def test_observe_request_parses_defaults_and_bounded_selectors():
    request = parse_observe_target_request(
        {
            "pid": 42,
            "selectors": [
                {"role": "button"},
                {"name": "Save", "match": "prefix"},
            ],
        }
    )

    assert request.target.pid == 42
    assert request.target.mode is TargetMode.FOREGROUND
    assert request.intent is TargetIntent.INSPECT
    assert request.deadline_ms == DEFAULT_TRANSACTION_DEADLINE_MS
    assert request.selectors[0].match is MatchMode.EXACT
    assert request.selectors[1].match is MatchMode.PREFIX


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"target_id": ""},
        {"pid": 0},
        {"query": "PR", "pid": 42},
        {"query": "PR", "target_id": "native:42"},
        {"pid": 42, "target_id": "native:42"},
    ],
)
def test_observe_requires_exactly_one_nonempty_target_selector(arguments):
    with pytest.raises(InputValidationError):
        parse_observe_target_request(arguments)


def test_observe_rejects_empty_locator_and_excess_selectors():
    with pytest.raises(InputValidationError, match="requires role"):
        parse_observe_target_request({"pid": 42, "selectors": [{}]})

    selectors = [{"role": "button"}] * (MAX_TRANSACTION_SELECTORS + 1)
    with pytest.raises(InputValidationError, match="at most 8 items"):
        parse_observe_target_request({"pid": 42, "selectors": selectors})


def test_execute_parses_typed_steps_without_retaining_raw_mapping():
    arguments = _execute_request()
    request = parse_execute_request(arguments)

    assert request.target.query == "pull request 42"
    assert request.target.mode is TargetMode.FOREGROUND
    assert request.steps[0].operation is TransactionOperation.LOCATE
    assert request.steps[0].alias == "comment"
    assert request.steps[0].locator is not None
    assert request.steps[1].expect is not None
    assert request.steps[-1].consequence == "external_write"
    assert request.final_expect is not None
    assert request.deadline_ms == 3000
    assert not hasattr(request, "raw_arguments")


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"query": "PR", "pid": 42},
        {"query": "PR", "target_id": "native:42"},
        {"pid": 42, "target_id": "native:42"},
    ],
)
def test_execute_requires_one_foreground_target_selector(target):
    arguments = _execute_request()
    arguments["target"] = target

    with pytest.raises(InputValidationError, match="exactly one"):
        parse_execute_request(arguments)


@pytest.mark.parametrize(
    "target",
    [
        {"mode": "shadow", "query": "PR"},
        {"mode": "shadow", "pid": 42},
        {"mode": "shadow", "target_id": "native:42"},
        {
            "mode": "shadow",
            "target_id": "cdp:42",
            "on_missing": "open",
            "url": "https://example.test",
        },
    ],
)
def test_shadow_requires_one_exact_shadow_target_without_open_fallback(target):
    arguments = _execute_request()
    arguments["target"] = target

    with pytest.raises(InputValidationError, match="shadow"):
        parse_execute_request(arguments)

    arguments["target"] = {"mode": "shadow", "target_id": "cdp:42"}
    request = parse_execute_request(arguments)
    assert request.target.mode is TargetMode.SHADOW


@pytest.mark.parametrize(
    "target",
    [
        {"query": "PR", "on_missing": "open"},
        {"query": "PR", "on_missing": "open", "url": "javascript:alert(1)"},
        {"query": "PR", "on_missing": "open", "url": "http://[invalid"},
        {
            "query": "PR",
            "snapshot": "n-old",
            "on_missing": "open",
            "url": "https://example.test",
        },
        {"pid": 42, "on_missing": "open", "url": "https://example.test"},
        {"target_id": "native:42", "url": "https://example.test"},
    ],
)
def test_open_if_missing_requires_browser_query_and_safe_explicit_url(target):
    arguments = _execute_request()
    arguments["target"] = target

    with pytest.raises(InputValidationError, match="on_missing=open"):
        parse_execute_request(arguments)

    arguments["target"] = {
        "query": "PR",
        "on_missing": "open",
        "url": "https://example.test/pull-requests/42",
    }
    request = parse_execute_request(arguments)
    assert request.target.open_if_missing is True


def test_execute_requires_between_one_and_eight_steps():
    arguments = _execute_request()
    arguments["steps"] = []
    with pytest.raises(InputValidationError, match="at least 1 items"):
        parse_execute_request(arguments)

    arguments["steps"] = [{"op": "scroll", "delta_y": 1}] * (MAX_TRANSACTION_STEPS + 1)
    with pytest.raises(InputValidationError, match="at most 8 items"):
        parse_execute_request(arguments)


@pytest.mark.parametrize(
    "step,message",
    [
        ({"op": "run_javascript"}, "allowed value"),
        ({"op": "locate", "as": "target"}, "requires role"),
        ({"op": "locate", "role": "button"}, "requires as"),
        ({"op": "click"}, "requires ref"),
        ({"op": "hover", "ref": "missing"}, "unknown ref"),
        ({"op": "type", "ref": "missing", "text": "x"}, "unknown ref"),
        ({"op": "press_key", "ref": "missing", "key": "Enter"}, "unknown ref"),
        ({"op": "scroll", "delta_x": 0, "delta_y": 0}, "non-zero delta"),
        ({"op": "expect"}, "requires role"),
    ],
)
def test_operation_specific_semantics_fail_closed(step, message):
    arguments = _execute_request()
    arguments["steps"] = [step]

    with pytest.raises(InputValidationError, match=message):
        parse_execute_request(arguments)


def test_aliases_are_unique_defined_before_use_and_scope_aware():
    arguments = _execute_request()
    arguments["steps"] = [
        {"op": "locate", "as": "row", "role": "group"},
        {
            "op": "locate",
            "as": "save",
            "within": "row",
            "role": "button",
            "name": "Save",
        },
        {"op": "click", "ref": "save"},
    ]
    request = parse_execute_request(arguments)
    assert request.steps[1].locator is not None
    assert request.steps[1].locator.within == "row"

    duplicate = deepcopy(arguments)
    duplicate["steps"].insert(
        1,
        {"op": "locate", "as": "row", "role": "group"},
    )
    with pytest.raises(InputValidationError, match="duplicate alias"):
        parse_execute_request(duplicate)

    forward = deepcopy(arguments)
    forward["steps"] = [
        {
            "op": "locate",
            "as": "save",
            "within": "row",
            "role": "button",
        },
        {"op": "locate", "as": "row", "role": "group"},
    ]
    with pytest.raises(InputValidationError, match="unknown within"):
        parse_execute_request(forward)


@pytest.mark.parametrize(
    "step",
    [
        {"op": "locate", "as": "field", "role": "textbox", "text": "x"},
        {"op": "click", "ref": "field", "role": "button"},
        {"op": "type", "ref": "field", "text": "x", "key": "Enter"},
        {"op": "press_key", "ref": "field", "key": "Enter", "delta_y": 1},
        {"op": "scroll", "delta_y": 1, "text": "x"},
        {"op": "expect", "role": "textbox", "ref": "field"},
    ],
)
def test_each_operation_rejects_fields_from_other_operation_shapes(step):
    arguments = _execute_request()
    base_alias = "base" if step["op"] == "locate" else "field"
    arguments["steps"] = [
        {"op": "locate", "as": base_alias, "role": "textbox"},
        step,
    ]

    with pytest.raises(InputValidationError, match="does not allow"):
        parse_execute_request(arguments)


def test_aliases_use_compact_identifier_syntax():
    arguments = _execute_request()
    arguments["steps"] = [{"op": "locate", "as": "not valid", "role": "button"}]

    with pytest.raises(InputValidationError, match="alias syntax"):
        parse_execute_request(arguments)


def test_shadow_transaction_accepts_hover_for_capable_explicit_provider():
    arguments = _execute_request()
    arguments["target"] = {"mode": "shadow", "target_id": "cdp:42"}
    arguments["steps"] = [
        {"op": "locate", "as": "field", "role": "textbox"},
        {"op": "hover", "ref": "field"},
    ]

    request = parse_execute_request(arguments)

    assert request.target.mode is TargetMode.SHADOW
    assert request.steps[1].operation is TransactionOperation.HOVER


def test_only_one_consequential_action_is_allowed_and_it_is_last_mutation():
    arguments = _execute_request()
    arguments["steps"] = [
        {"op": "locate", "as": "save", "role": "button"},
        {
            "op": "click",
            "ref": "save",
            "consequence": "external_write",
        },
        {"op": "expect", "role": "article", "name": "posted"},
    ]
    request = parse_execute_request(arguments)
    assert request.consequential_step == 1

    second_write = deepcopy(arguments)
    second_write["steps"].insert(
        2,
        {
            "op": "press_key",
            "ref": "save",
            "key": "Enter",
            "consequence": "external_write",
        },
    )
    with pytest.raises(InputValidationError, match="at most one consequential"):
        parse_execute_request(second_write)

    later_mutation = deepcopy(arguments)
    later_mutation["steps"].append({"op": "scroll", "delta_y": 100})
    with pytest.raises(InputValidationError, match="last mutating step"):
        parse_execute_request(later_mutation)


def test_consequence_is_rejected_on_non_action_steps():
    arguments = _execute_request()
    arguments["steps"] = [
        {
            "op": "locate",
            "as": "save",
            "role": "button",
            "consequence": "external_write",
        }
    ]

    with pytest.raises(InputValidationError, match="consequence"):
        parse_execute_request(arguments)


def test_validation_errors_never_echo_typed_or_locator_values():
    secret = "SENTINEL-super-secret-comment"
    arguments = _execute_request()
    arguments["steps"] = [
        {
            "op": "type",
            "ref": secret,
            "text": secret,
        }
    ]

    with pytest.raises(InputValidationError) as exc_info:
        parse_execute_request(arguments)

    assert secret not in str(exc_info.value)


def test_transaction_error_codes_are_stable_and_distinct():
    assert OperationErrorCode.INVALID_TRANSACTION.value == "INVALID_TRANSACTION"
    assert OperationErrorCode.AMBIGUOUS_ELEMENT.value == "AMBIGUOUS_ELEMENT"
    assert OperationErrorCode.OUTCOME_UNKNOWN.value == "OUTCOME_UNKNOWN"
