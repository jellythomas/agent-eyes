"""Aggregate sanitized reference-agent JSON or JSONL timing records."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any


_SCHEMA_VERSION = 1
_STRETCH_MEDIAN_MS = 3_000.0
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "run",
        "agent_eyes_ms",
        "model_ms",
        "client_ms",
        "mcp_calls",
    }
)
_DURATION_FIELDS = ("agent_eyes_ms", "model_ms", "client_ms")


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _decode_records(text: str) -> list[Any]:
    if not text.strip():
        raise ValueError("reference trace must contain at least one record")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"reference trace line {line_number} is not valid JSON"
                ) from exc
        return records
    if isinstance(payload, list):
        return payload
    return [payload]


def _positive_integer(value: object, *, field: str, index: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"record {index} {field} must be a positive integer")
    return value


def _duration(value: object, *, field: str, index: int) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"record {index} {field} must be a finite non-negative number")
    return float(value)


def _validate_records(payloads: list[Any]) -> list[dict[str, int | float]]:
    if not payloads:
        raise ValueError("reference trace must contain at least one record")
    records: list[dict[str, int | float]] = []
    run_ids: set[int] = set()
    for index, payload in enumerate(payloads, start=1):
        if not isinstance(payload, dict):
            raise ValueError(f"record {index} must be a JSON object")
        fields = set(payload)
        missing = _RECORD_FIELDS - fields
        unexpected = fields - _RECORD_FIELDS
        if missing:
            raise ValueError(
                f"record {index} is missing fields: {', '.join(sorted(missing))}"
            )
        if unexpected:
            raise ValueError(
                f"record {index} has unexpected fields: {', '.join(sorted(unexpected))}"
            )
        schema_version = _positive_integer(
            payload["schema_version"],
            field="schema_version",
            index=index,
        )
        if schema_version != _SCHEMA_VERSION:
            raise ValueError(f"record {index} schema_version must be {_SCHEMA_VERSION}")
        run = _positive_integer(payload["run"], field="run", index=index)
        if run in run_ids:
            raise ValueError(f"record {index} has duplicate run identifier {run}")
        run_ids.add(run)
        record: dict[str, int | float] = {
            "schema_version": schema_version,
            "run": run,
            "mcp_calls": _positive_integer(
                payload["mcp_calls"],
                field="mcp_calls",
                index=index,
            ),
        }
        for field in _DURATION_FIELDS:
            record[field] = _duration(payload[field], field=field, index=index)
        records.append(record)
    return records


def analyze_trace(path: Path) -> dict[str, object]:
    """Validate one content-free trace and summarize each timing plane."""
    records = _validate_records(
        _decode_records(path.resolve(strict=True).read_text(encoding="utf-8"))
    )
    agent_eyes = [float(record["agent_eyes_ms"]) for record in records]
    model = [float(record["model_ms"]) for record in records]
    client = [float(record["client_ms"]) for record in records]
    end_to_end = [
        agent_eyes_ms + model_ms + client_ms
        for agent_eyes_ms, model_ms, client_ms in zip(agent_eyes, model, client)
    ]
    end_to_end_summary = _summary(end_to_end)
    return {
        "schema_version": _SCHEMA_VERSION,
        "protocol": {
            "records": len(records),
            "percentile": "sorted[ceil(0.95*n)-1]",
            "durations_separated": True,
            "reference_agent_median_stretch_ms": _STRETCH_MEDIAN_MS,
            "release_blocking": False,
        },
        "durations": {
            "agent_eyes": _summary(agent_eyes),
            "model": _summary(model),
            "client": _summary(client),
            "end_to_end": end_to_end_summary,
        },
        "evidence": {
            "mcp_calls": sorted({int(record["mcp_calls"]) for record in records})
        },
        "stretch_target": {
            "median_ms": _STRETCH_MEDIAN_MS,
            "observed_median_ms": end_to_end_summary["median_ms"],
            "passed": end_to_end_summary["median_ms"] <= _STRETCH_MEDIAN_MS,
            "release_blocking": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    arguments = parser.parse_args()
    try:
        result = analyze_trace(arguments.trace)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
