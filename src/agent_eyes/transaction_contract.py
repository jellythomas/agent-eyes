"""Bounded public contracts for compact observations and UI transactions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any
from urllib.parse import urlsplit

from .input_validation import InputValidationError, validate_tool_arguments
from .operation import OperationErrorCode
from .tool_contract import (
    MAX_QUERY_CHARS,
    MAX_TYPED_TEXT_CHARS,
    MAX_URL_CHARS,
)


MAX_TRANSACTION_STEPS = 8
MAX_TRANSACTION_SELECTORS = 8
DEFAULT_TRANSACTION_DEADLINE_MS = 3_000
MAX_TRANSACTION_DEADLINE_MS = 30_000
MAX_TRANSACTION_RESULTS = 20
MAX_ALIAS_CHARS = 64
MAX_LOCATOR_TEXT_CHARS = 512
MAX_KEY_CHARS = 64
MAX_SCROLL_DELTA = 10_000

_SAFE_OPEN_SCHEMES = frozenset({"about", "http", "https"})
_MUTATING_OPERATIONS = frozenset({"hover", "click", "type", "press_key", "scroll"})
_CONSEQUENTIAL_OPERATIONS = frozenset({"click", "type", "press_key"})
_ALIAS_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_STEP_FIELDS = {
    "locate": frozenset({"op", "as", "role", "name", "value", "match", "within"}),
    "hover": frozenset({"op", "ref", "expect"}),
    "click": frozenset({"op", "ref", "expect", "consequence"}),
    "type": frozenset({"op", "ref", "text", "expect", "consequence"}),
    "press_key": frozenset({"op", "ref", "key", "expect", "consequence"}),
    "scroll": frozenset({"op", "ref", "delta_x", "delta_y", "expect"}),
    "expect": frozenset({"op", "role", "name", "value", "match", "within"}),
}


class TargetMode(Enum):
    FOREGROUND = "foreground"
    SHADOW = "shadow"


class TargetIntent(Enum):
    INSPECT = "inspect"
    INTERACT = "interact"


class MatchMode(Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    PREFIX = "prefix"
    SUFFIX = "suffix"


class TransactionOperation(Enum):
    LOCATE = "locate"
    HOVER = "hover"
    CLICK = "click"
    TYPE = "type"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
    EXPECT = "expect"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    mode: TargetMode
    target_id: str = ""
    pid: int | None = None
    query: str = ""
    snapshot: str = ""
    url: str = ""
    open_if_missing: bool = False


@dataclass(frozen=True, slots=True)
class Locator:
    role: str = ""
    name: str = ""
    value: str = ""
    match: MatchMode = MatchMode.EXACT
    within: str = ""


@dataclass(frozen=True, slots=True)
class ObserveTargetRequest:
    target: TargetSpec
    intent: TargetIntent
    selectors: tuple[Locator, ...]
    max_results: int
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class TransactionStep:
    index: int
    operation: TransactionOperation
    alias: str = ""
    ref: str = ""
    locator: Locator | None = None
    text: str = ""
    key: str = ""
    delta_x: int = 0
    delta_y: int = 0
    expect: Locator | None = None
    consequence: str = ""


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    target: TargetSpec
    steps: tuple[TransactionStep, ...]
    final_expect: Locator | None
    deadline_ms: int
    consequential_step: int | None


def _locator_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "role": {"type": "string", "maxLength": 128},
            "name": {"type": "string", "maxLength": MAX_LOCATOR_TEXT_CHARS},
            "value": {"type": "string", "maxLength": MAX_LOCATOR_TEXT_CHARS},
            "match": {
                "type": "string",
                "enum": [mode.value for mode in MatchMode],
            },
            "within": {"type": "string", "maxLength": MAX_ALIAS_CHARS},
        },
        "additionalProperties": False,
    }


def build_observe_target_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
            },
            "target_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "pid": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
            "intent": {
                "type": "string",
                "enum": [intent.value for intent in TargetIntent],
            },
            "selectors": {
                "type": "array",
                "maxItems": MAX_TRANSACTION_SELECTORS,
                "items": _locator_schema(),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TRANSACTION_RESULTS,
            },
            "deadline_ms": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TRANSACTION_DEADLINE_MS,
            },
        },
        "additionalProperties": False,
    }


def _target_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
            },
            "target_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "pid": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
            "snapshot": {"type": "string", "minLength": 1, "maxLength": 128},
            "mode": {
                "type": "string",
                "enum": [mode.value for mode in TargetMode],
            },
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_URL_CHARS,
            },
            "on_missing": {
                "type": "string",
                "enum": ["fail", "open"],
            },
        },
        "additionalProperties": False,
    }


def _step_schema() -> dict[str, Any]:
    locator = _locator_schema()
    return {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": [operation.value for operation in TransactionOperation],
            },
            "as": {"type": "string", "minLength": 1, "maxLength": MAX_ALIAS_CHARS},
            "ref": {"type": "string", "minLength": 1, "maxLength": MAX_ALIAS_CHARS},
            "role": locator["properties"]["role"],
            "name": locator["properties"]["name"],
            "value": locator["properties"]["value"],
            "match": locator["properties"]["match"],
            "within": locator["properties"]["within"],
            "text": {"type": "string", "maxLength": MAX_TYPED_TEXT_CHARS},
            "key": {"type": "string", "minLength": 1, "maxLength": MAX_KEY_CHARS},
            "delta_x": {
                "type": "integer",
                "minimum": -MAX_SCROLL_DELTA,
                "maximum": MAX_SCROLL_DELTA,
            },
            "delta_y": {
                "type": "integer",
                "minimum": -MAX_SCROLL_DELTA,
                "maximum": MAX_SCROLL_DELTA,
            },
            "expect": _locator_schema(),
            "consequence": {"type": "string", "enum": ["external_write"]},
        },
        "required": ["op"],
        "additionalProperties": False,
    }


def build_execute_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "target": _target_schema(),
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_TRANSACTION_STEPS,
                "items": _step_schema(),
            },
            "expect": _locator_schema(),
            "deadline_ms": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TRANSACTION_DEADLINE_MS,
            },
        },
        "required": ["target", "steps"],
        "additionalProperties": False,
    }


OBSERVE_TARGET_INPUT_SCHEMA = build_observe_target_input_schema()
EXECUTE_INPUT_SCHEMA = build_execute_input_schema()


def _invalid(message: str) -> InputValidationError:
    return InputValidationError(
        f"{OperationErrorCode.INVALID_TRANSACTION.value}: {message}"
    )


def _validate_contract(schema: dict[str, Any], arguments: Any) -> None:
    try:
        validate_tool_arguments(schema, arguments)
    except InputValidationError as exc:
        # The dependency-free validator reports paths and constraints only; it
        # never includes caller values. Preserve that safe detail under one
        # stable transaction error code.
        raise _invalid(str(exc)) from exc


def _parse_locator(
    value: dict[str, Any],
    *,
    known_aliases: set[str] | None = None,
    location: str = "locator",
) -> Locator:
    role = str(value.get("role", ""))
    name = str(value.get("name", ""))
    locator_value = str(value.get("value", ""))
    if not role and not name and not locator_value:
        raise _invalid(f"{location} requires role, name, or value")
    within = str(value.get("within", ""))
    if within and known_aliases is not None and within not in known_aliases:
        raise _invalid(f"{location} has an unknown within reference")
    return Locator(
        role=role,
        name=name,
        value=locator_value,
        match=MatchMode(str(value.get("match", MatchMode.EXACT.value))),
        within=within,
    )


def _parse_target(value: dict[str, Any]) -> TargetSpec:
    mode = TargetMode(str(value.get("mode", TargetMode.FOREGROUND.value)))
    target_id = str(value.get("target_id", ""))
    query = str(value.get("query", ""))
    pid = value.get("pid")
    selectors = int(bool(target_id)) + int(bool(query)) + int(pid is not None)
    if selectors != 1:
        raise _invalid("target requires exactly one of target_id, pid, or query")

    snapshot = str(value.get("snapshot", ""))
    url = str(value.get("url", ""))
    open_if_missing = value.get("on_missing", "fail") == "open"

    if mode is TargetMode.SHADOW:
        if not target_id or target_id.startswith("native:"):
            raise _invalid("shadow mode requires one exact shadow target_id")
        if pid is not None or query or url or open_if_missing:
            raise _invalid(
                "shadow mode does not allow query, pid, URL, or open fallback"
            )
    elif target_id and not target_id.startswith("native:"):
        raise _invalid("foreground target_id must be provider-qualified as native")

    if open_if_missing:
        if not query or not url or pid is not None or target_id or snapshot:
            raise _invalid("on_missing=open requires a browser query and explicit URL")
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise _invalid(
                "on_missing=open requires an allowed explicit URL scheme"
            ) from exc
        if parsed.scheme.casefold() not in _SAFE_OPEN_SCHEMES:
            raise _invalid("on_missing=open requires an allowed explicit URL scheme")
    elif url:
        raise _invalid("URL is allowed only with on_missing=open")

    return TargetSpec(
        mode=mode,
        target_id=target_id,
        pid=pid,
        query=query,
        snapshot=snapshot,
        url=url,
        open_if_missing=open_if_missing,
    )


def parse_observe_target_request(arguments: Any) -> ObserveTargetRequest:
    _validate_contract(OBSERVE_TARGET_INPUT_SCHEMA, arguments)
    if not isinstance(arguments, dict):
        raise _invalid("observe_target arguments must be an object")

    target = _parse_target(arguments)
    if target.mode is not TargetMode.FOREGROUND:
        raise _invalid("observe_target currently supports foreground targets only")

    raw_selectors = arguments.get("selectors", [])
    if len(raw_selectors) > MAX_TRANSACTION_SELECTORS:
        raise _invalid("observe_target accepts at most 8 selectors")
    selectors = tuple(
        _parse_locator(selector, location=f"selector {index + 1}")
        for index, selector in enumerate(raw_selectors)
    )
    return ObserveTargetRequest(
        target=target,
        intent=TargetIntent(str(arguments.get("intent", TargetIntent.INSPECT.value))),
        selectors=selectors,
        max_results=int(arguments.get("max_results", 10)),
        deadline_ms=int(arguments.get("deadline_ms", DEFAULT_TRANSACTION_DEADLINE_MS)),
    )


def _parse_step(
    value: dict[str, Any],
    *,
    index: int,
    known_aliases: set[str],
) -> TransactionStep:
    operation = TransactionOperation(str(value["op"]))
    consequence = str(value.get("consequence", ""))
    if consequence and operation.value not in _CONSEQUENTIAL_OPERATIONS:
        raise _invalid("consequence is allowed only on a dispatching action")
    if set(value) - _STEP_FIELDS[operation.value]:
        raise _invalid(f"{operation.value} does not allow one or more supplied fields")
    alias = str(value.get("as", ""))
    ref = str(value.get("ref", ""))
    locator: Locator | None = None

    if operation is TransactionOperation.LOCATE:
        if not alias:
            raise _invalid("locate requires as")
        if _ALIAS_PATTERN.fullmatch(alias) is None:
            raise _invalid("locate alias syntax must be a compact identifier")
        if alias in known_aliases:
            raise _invalid("transaction contains a duplicate alias")
        locator = _parse_locator(
            value,
            known_aliases=known_aliases,
            location="locate locator",
        )
    elif operation in {
        TransactionOperation.HOVER,
        TransactionOperation.CLICK,
        TransactionOperation.TYPE,
        TransactionOperation.PRESS_KEY,
    }:
        if not ref:
            raise _invalid(f"{operation.value} requires ref")
        if ref not in known_aliases:
            raise _invalid(f"{operation.value} has an unknown ref")
    elif operation is TransactionOperation.SCROLL:
        if ref and ref not in known_aliases:
            raise _invalid("scroll has an unknown ref")
        if int(value.get("delta_x", 0)) == 0 and int(value.get("delta_y", 0)) == 0:
            raise _invalid("scroll requires a non-zero delta")
    elif operation is TransactionOperation.EXPECT:
        locator = _parse_locator(
            value,
            known_aliases=known_aliases,
            location="expect locator",
        )

    if operation is TransactionOperation.TYPE and "text" not in value:
        raise _invalid("type requires text")
    if operation is TransactionOperation.PRESS_KEY and not value.get("key"):
        raise _invalid("press_key requires key")

    nested_expect = value.get("expect")
    expect = (
        _parse_locator(
            nested_expect,
            known_aliases=known_aliases,
            location="step expectation",
        )
        if nested_expect is not None
        else None
    )

    return TransactionStep(
        index=index,
        operation=operation,
        alias=alias,
        ref=ref,
        locator=locator,
        text=str(value.get("text", "")),
        key=str(value.get("key", "")),
        delta_x=int(value.get("delta_x", 0)),
        delta_y=int(value.get("delta_y", 0)),
        expect=expect,
        consequence=consequence,
    )


def parse_execute_request(arguments: Any) -> ExecuteRequest:
    _validate_contract(EXECUTE_INPUT_SCHEMA, arguments)
    if not isinstance(arguments, dict):
        raise _invalid("execute arguments must be an object")

    raw_steps = arguments["steps"]
    if not raw_steps:
        raise _invalid("execute requires at least 1 step")
    if len(raw_steps) > MAX_TRANSACTION_STEPS:
        raise _invalid("execute accepts at most 8 steps")

    target = _parse_target(arguments["target"])
    known_aliases: set[str] = set()
    steps: list[TransactionStep] = []
    consequence_indices: list[int] = []

    for index, raw_step in enumerate(raw_steps):
        step = _parse_step(raw_step, index=index, known_aliases=known_aliases)
        steps.append(step)
        if step.consequence:
            consequence_indices.append(index)
        if step.alias:
            known_aliases.add(step.alias)

    if len(consequence_indices) > 1:
        raise _invalid("transaction allows at most one consequential action")
    consequential_step = consequence_indices[0] if consequence_indices else None
    if consequential_step is not None:
        for step in steps[consequential_step + 1 :]:
            if step.operation.value in _MUTATING_OPERATIONS:
                raise _invalid("consequential action must be the last mutating step")

    final_expect = (
        _parse_locator(
            arguments["expect"],
            known_aliases=known_aliases,
            location="final expectation",
        )
        if "expect" in arguments
        else None
    )
    return ExecuteRequest(
        target=target,
        steps=tuple(steps),
        final_expect=final_expect,
        deadline_ms=int(arguments.get("deadline_ms", DEFAULT_TRANSACTION_DEADLINE_MS)),
        consequential_step=consequential_step,
    )
