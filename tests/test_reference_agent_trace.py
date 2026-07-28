from __future__ import annotations

import json

import pytest

from benchmarks import analyze_reference_agent_trace


def _record(
    run: int,
    *,
    agent_eyes_ms: float,
    model_ms: float,
    client_ms: float,
    mcp_calls: int = 1,
) -> dict[str, int | float]:
    return {
        "schema_version": 1,
        "run": run,
        "agent_eyes_ms": agent_eyes_ms,
        "model_ms": model_ms,
        "client_ms": client_ms,
        "mcp_calls": mcp_calls,
    }


def test_json_array_reports_each_duration_plane_separately(tmp_path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        json.dumps(
            [
                _record(1, agent_eyes_ms=100, model_ms=300, client_ms=50),
                _record(2, agent_eyes_ms=200, model_ms=500, client_ms=100),
            ]
        ),
        encoding="utf-8",
    )

    result = analyze_reference_agent_trace.analyze_trace(trace_path)

    assert result["protocol"] == {
        "records": 2,
        "percentile": "sorted[ceil(0.95*n)-1]",
        "durations_separated": True,
        "reference_agent_median_stretch_ms": 3_000.0,
        "release_blocking": False,
    }
    assert result["durations"]["agent_eyes"] == {
        "median_ms": 150.0,
        "p95_ms": 200.0,
        "max_ms": 200.0,
    }
    assert result["durations"]["model"] == {
        "median_ms": 400.0,
        "p95_ms": 500.0,
        "max_ms": 500.0,
    }
    assert result["durations"]["client"] == {
        "median_ms": 75.0,
        "p95_ms": 100.0,
        "max_ms": 100.0,
    }
    assert result["durations"]["end_to_end"] == {
        "median_ms": 625.0,
        "p95_ms": 800.0,
        "max_ms": 800.0,
    }
    assert result["evidence"] == {"mcp_calls": [1]}
    assert result["stretch_target"] == {
        "median_ms": 3_000.0,
        "observed_median_ms": 625.0,
        "passed": True,
        "release_blocking": False,
    }


def test_json_lines_uses_the_same_strict_record_schema(tmp_path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                _record(1, agent_eyes_ms=10, model_ms=20, client_ms=30),
                _record(2, agent_eyes_ms=40, model_ms=50, client_ms=60),
            )
        ),
        encoding="utf-8",
    )

    result = analyze_reference_agent_trace.analyze_trace(trace_path)

    assert result["protocol"]["records"] == 2
    assert result["durations"]["end_to_end"]["median_ms"] == 105.0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"query": "confidential pull request"}, "unexpected fields"),
        ({"agent_eyes_ms": -1}, "agent_eyes_ms"),
        ({"model_ms": True}, "model_ms"),
        ({"client_ms": float("inf")}, "client_ms"),
        ({"mcp_calls": 0}, "mcp_calls"),
    ],
)
def test_trace_rejects_content_fields_and_invalid_counts(
    tmp_path,
    mutation,
    message,
) -> None:
    record = _record(1, agent_eyes_ms=10, model_ms=20, client_ms=30)
    record.update(mutation)
    trace_path = tmp_path / "invalid.json"
    trace_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        analyze_reference_agent_trace.analyze_trace(trace_path)


def test_trace_rejects_duplicate_run_identifiers(tmp_path) -> None:
    trace_path = tmp_path / "duplicate.json"
    trace_path.write_text(
        json.dumps(
            [
                _record(1, agent_eyes_ms=10, model_ms=20, client_ms=30),
                _record(1, agent_eyes_ms=40, model_ms=50, client_ms=60),
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate run"):
        analyze_reference_agent_trace.analyze_trace(trace_path)
