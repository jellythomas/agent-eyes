"""Global UTF-8-safe output budgets for MCP tool results."""
from __future__ import annotations

import json
from dataclasses import dataclass


_TEXT_CHUNK_CHARACTERS = 4 * 1024


def _positive_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ResultBudget:
    default_bytes: int = 16 * 1024
    hard_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        default_bytes = _positive_integer(
            self.default_bytes,
            name="default_bytes",
        )
        hard_bytes = _positive_integer(self.hard_bytes, name="hard_bytes")
        if default_bytes > hard_bytes:
            raise ValueError("default_bytes must not exceed hard_bytes")


@dataclass(frozen=True, slots=True)
class BoundedResult:
    text: str
    truncated: bool
    original_bytes: int
    returned_bytes: int


@dataclass(frozen=True, slots=True)
class BoundedJsonSize:
    """Exact serialized size, a lower bound, or an unavailable-size state."""

    bytes: int | None
    exact: bool


class _JsonLimitExceeded(Exception):
    pass


class _BoundedJsonCounter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self._containers: set[int] = set()

    def add(self, size: int) -> None:
        self.total += size
        if self.total > self.limit:
            raise _JsonLimitExceeded

    def count(self, value: object) -> None:
        if value is None:
            self.add(4)
            return
        if value is True:
            self.add(4)
            return
        if value is False:
            self.add(5)
            return
        if isinstance(value, str):
            self._count_string(value)
            return
        if isinstance(value, (int, float)):
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            self.add(len(rendered.encode("utf-8")))
            return
        if isinstance(value, (list, tuple)):
            self._count_sequence(value)
            return
        if isinstance(value, dict):
            self._count_mapping(value)
            return
        raise TypeError(f"unsupported JSON value: {type(value).__name__}")

    def _count_string(self, value: str) -> None:
        self.add(1)
        for offset in range(0, len(value), _TEXT_CHUNK_CHARACTERS):
            chunk = value[offset : offset + _TEXT_CHUNK_CHARACTERS]
            rendered = json.dumps(
                chunk,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            # The surrounding quotes are counted once for the complete value.
            encoded = rendered.encode("utf-8")
            self.add(len(encoded) - 2)
        self.add(1)

    def _enter_container(self, value: object) -> int:
        identity = id(value)
        if identity in self._containers:
            raise ValueError("circular JSON value")
        self._containers.add(identity)
        return identity

    def _count_sequence(self, value: list | tuple) -> None:
        identity = self._enter_container(value)
        try:
            self.add(1)
            for index, item in enumerate(value):
                if index:
                    self.add(1)
                self.count(item)
            self.add(1)
        finally:
            self._containers.discard(identity)

    def _count_mapping(self, value: dict) -> None:
        identity = self._enter_container(value)
        try:
            self.add(1)
            for index, (key, item) in enumerate(value.items()):
                if index:
                    self.add(1)
                self._count_string(self._json_key(key))
                self.add(1)
                self.count(item)
            self.add(1)
        finally:
            self._containers.discard(identity)

    @staticmethod
    def _json_key(key: object) -> str:
        if isinstance(key, str):
            return key
        if key is True:
            return "true"
        if key is False:
            return "false"
        if key is None:
            return "null"
        if isinstance(key, (int, float)):
            return json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        raise TypeError(f"unsupported JSON key: {type(key).__name__}")


def bounded_json_utf8_size(
    value: object,
    *,
    exact_limit: int = 64 * 1024,
) -> BoundedJsonSize:
    """Count compact UTF-8 JSON with bounded traversal and temporary memory."""
    limit = _positive_integer(exact_limit, name="exact_limit")
    counter = _BoundedJsonCounter(limit)
    try:
        counter.count(value)
    except _JsonLimitExceeded:
        return BoundedJsonSize(bytes=limit + 1, exact=False)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return BoundedJsonSize(bytes=None, exact=False)
    return BoundedJsonSize(bytes=counter.total, exact=True)


class BoundedResultFormatter:
    def __init__(self, budget: ResultBudget | None = None) -> None:
        self._budget = budget or ResultBudget()

    def format(self, text: str, *, byte_limit: int | None = None) -> BoundedResult:
        if not isinstance(text, str):
            raise TypeError("tool result must be text")
        if byte_limit is not None:
            _positive_integer(byte_limit, name="byte_limit")
        requested = self._budget.default_bytes if byte_limit is None else byte_limit
        limit = min(requested, self._budget.hard_bytes)

        original_bytes = 0
        bounded_prefix = bytearray()
        for offset in range(0, len(text), _TEXT_CHUNK_CHARACTERS):
            encoded = text[offset : offset + _TEXT_CHUNK_CHARACTERS].encode(
                "utf-8",
                errors="replace",
            )
            original_bytes += len(encoded)
            if len(bounded_prefix) < limit:
                bounded_prefix.extend(encoded[: limit - len(bounded_prefix)])

        if original_bytes <= limit:
            rendered = bounded_prefix.decode("utf-8", errors="replace")
            returned_bytes = len(rendered.encode("utf-8"))
            return BoundedResult(
                text=rendered,
                truncated=False,
                original_bytes=original_bytes,
                returned_bytes=returned_bytes,
            )

        suffix = f"\n… [truncated {original_bytes}B to {limit}B]"
        suffix_bytes = suffix.encode("utf-8")
        if len(suffix_bytes) >= limit:
            rendered = self._utf8_prefix(suffix_bytes, limit)
        else:
            prefix = self._utf8_prefix(
                bounded_prefix,
                limit - len(suffix_bytes),
            )
            rendered = prefix + suffix
        returned_bytes = len(rendered.encode("utf-8"))
        return BoundedResult(
            text=rendered,
            truncated=True,
            original_bytes=original_bytes,
            returned_bytes=returned_bytes,
        )

    @staticmethod
    def _utf8_prefix(value: bytes | bytearray, limit: int) -> str:
        return bytes(value[:limit]).decode("utf-8", errors="ignore")
