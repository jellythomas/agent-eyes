from __future__ import annotations

import json
import tracemalloc

import pytest

from agent_eyes.result_format import (
    BoundedResultFormatter,
    ResultBudget,
    bounded_json_utf8_size,
)


def test_small_result_is_returned_unchanged():
    result = BoundedResultFormatter().format("ready")

    assert result.text == "ready"
    assert result.truncated is False
    assert result.original_bytes == 5
    assert result.returned_bytes == 5


def test_default_budget_caps_output_and_adds_metadata():
    formatter = BoundedResultFormatter(ResultBudget(default_bytes=128, hard_bytes=256))
    result = formatter.format("x" * 1_000)

    assert result.truncated is True
    assert result.original_bytes == 1_000
    assert result.returned_bytes <= 128
    assert len(result.text.encode("utf-8")) <= 128
    assert "truncated" in result.text
    assert "1000" in result.text


def test_requested_budget_can_expand_only_to_hard_ceiling():
    formatter = BoundedResultFormatter(ResultBudget(default_bytes=64, hard_bytes=128))
    result = formatter.format("z" * 10_000, byte_limit=1_000_000)

    assert result.returned_bytes <= 128
    assert len(result.text.encode("utf-8")) <= 128


def test_unicode_truncation_never_returns_invalid_utf8_or_exceeds_limit():
    formatter = BoundedResultFormatter(ResultBudget(default_bytes=97, hard_bytes=128))
    result = formatter.format("🙂" * 1_000)

    encoded = result.text.encode("utf-8")
    assert encoded.decode("utf-8") == result.text
    assert len(encoded) <= 97
    assert result.truncated is True


def test_exact_boundary_is_not_truncated():
    formatter = BoundedResultFormatter(ResultBudget(default_bytes=8, hard_bytes=16))
    result = formatter.format("12345678")

    assert result.text == "12345678"
    assert result.truncated is False


def test_invalid_budget_configuration_is_rejected():
    for default_bytes, hard_bytes in (
        (0, 1),
        (2, 1),
        (-1, 10),
        (1.5, 10),
        (1, float("nan")),
        (1, float("inf")),
        (True, 10),
    ):
        try:
            ResultBudget(default_bytes=default_bytes, hard_bytes=hard_bytes)
        except ValueError:
            continue
        raise AssertionError("invalid result budget was accepted")


def test_json_size_is_exact_for_small_unicode_values():
    result = bounded_json_utf8_size({"emoji": "🙂", "items": [1, True, None]})

    assert result.bytes == 38
    assert result.exact is True


def test_json_size_stops_after_a_bounded_prefix_of_a_large_value():
    class GuardedList(list):
        def __iter__(self):
            for index, item in enumerate(super().__iter__()):
                if index >= 10:
                    raise AssertionError("large JSON value was fully traversed")
                yield item

    result = bounded_json_utf8_size(GuardedList(["x" * 2_048] * 100), exact_limit=8_192)

    assert result.bytes == 8_193
    assert result.exact is False


def test_json_size_rejects_invalid_limit():
    for limit in (0, -1, True, 1.5, float("nan"), float("inf"), "10"):
        try:
            bounded_json_utf8_size({}, exact_limit=limit)
        except ValueError:
            continue
        raise AssertionError("invalid JSON size limit was accepted")


def test_javascript_result_summary_keeps_small_sizes_exact_and_bounds_large_values():
    from agent_eyes.server import _javascript_result_summary

    assert _javascript_result_summary({"ready": True}) == (
        "JavaScript completed (result type dict, 14 bytes)."
    )
    assert _javascript_result_summary(["x" * 2_048] * 100) == (
        "JavaScript completed (result type list, >65536 bytes)."
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        42,
        -1.5,
        "quote \" slash \\ emoji 🙂 newline\n",
        [1, "two", None, [False]],
        {"emoji": "🙂", 1: "integer key", None: "none key"},
    ],
)
def test_json_size_matches_compact_standard_encoder(value):
    expected = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    result = bounded_json_utf8_size(value)

    assert result.bytes == len(expected)
    assert result.exact is True


@pytest.mark.parametrize("invalid", [object(), "\ud800"])
def test_json_size_reports_unavailable_for_unserializable_values(invalid):
    result = bounded_json_utf8_size(invalid)

    assert result.bytes is None
    assert result.exact is False


def test_json_size_reports_unavailable_for_circular_values():
    circular = []
    circular.append(circular)

    result = bounded_json_utf8_size(circular)

    assert result.bytes is None
    assert result.exact is False


def test_javascript_summary_never_claims_invalid_json_is_zero_bytes():
    from agent_eyes.server import _javascript_result_summary

    assert _javascript_result_summary(object()) == (
        "JavaScript completed (result type object, size unavailable)."
    )


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), True, "10"])
def test_formatter_rejects_non_integer_byte_limits(value):
    with pytest.raises(ValueError):
        BoundedResultFormatter().format("value", byte_limit=value)


def test_formatter_replaces_lone_surrogates_with_valid_utf8():
    result = BoundedResultFormatter().format("before\ud800after")

    assert result.text.encode("utf-8").decode("utf-8") == result.text
    assert "\ud800" not in result.text


@pytest.mark.parametrize("value", ["x" * 10_000_000, "\\" * 10_000_000])
def test_json_size_peak_memory_is_bounded_for_one_huge_string(value):
    tracemalloc.start()
    try:
        result = bounded_json_utf8_size(value, exact_limit=64 * 1024)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.bytes == 64 * 1024 + 1
    assert result.exact is False
    assert peak < 1_000_000


def test_formatter_peak_memory_is_bounded_for_huge_text():
    value = "x" * 20_000_000
    tracemalloc.start()
    try:
        result = BoundedResultFormatter().format(value)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.returned_bytes <= 16 * 1024
    assert result.original_bytes == len(value)
    assert peak < 1_000_000
