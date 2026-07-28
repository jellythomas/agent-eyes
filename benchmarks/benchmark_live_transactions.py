"""Benchmark live native browser targeting through one real MCP stdio session."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import time
from typing import TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REFERENCE_MEDIAN_GATE_MS = 1_000.0
REFERENCE_P95_GATE_MS = 3_000.0
OBSERVE_TARGET_OUTPUT_GATE_BYTES = 4 * 1024
EXECUTE_OUTPUT_GATE_BYTES = 2 * 1024
_TARGET_COUNT_PATTERN = re.compile(r"\AScanned (?P<count>[0-9]+) open browser targets")
_TARGET_ID_PATTERN = re.compile(r"\[(?P<id>native:[^\]]+)\]")
_TARGET_SLOT_PATTERN = re.compile(r"native:[0-9]+:w[0-9]+(?::t[0-9]+)?\Z")
_BROWSER_VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")
_ERROR_CODE_PATTERN = re.compile(r"\AERROR:\s*(?P<code>[A-Z][A-Z_]+):")
_STABLE_CODE_PATTERN = re.compile(r"[A-Z][A-Z_]+\Z")


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _deadline(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > 30_000:
        raise argparse.ArgumentTypeError("must be at most 30000")
    return parsed


def _browser_version(value: str) -> str:
    if not value:
        return ""
    if not _BROWSER_VERSION_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a compact browser version")
    return value


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[p95_index], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _tool_text(result) -> str:
    content = getattr(result, "content", ())
    if len(content) != 1:
        raise RuntimeError("live MCP tool call failed")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise RuntimeError("live MCP tool did not return one text result")
    if getattr(result, "isError", False):
        match = _ERROR_CODE_PATTERN.match(text)
        code = match.group("code") if match is not None else ""
        if not code:
            try:
                payload = json.loads(text.removeprefix("ERROR: "))
            except (TypeError, ValueError):
                payload = None
            candidate = payload.get("code") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and _STABLE_CODE_PATTERN.fullmatch(candidate):
                code = candidate
        code = code or "UNKNOWN"
        raise RuntimeError(f"live MCP tool call failed: {code}")
    return text


def _tool_json(result, *, expected_status: str = "ok") -> dict[str, object]:
    try:
        payload = json.loads(_tool_text(result))
    except json.JSONDecodeError as exc:
        raise RuntimeError("live MCP tool did not return JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != expected_status:
        raise RuntimeError("live MCP transaction did not succeed")
    return payload


def _inventory_evidence(text: str) -> tuple[int | None, str]:
    lines = text.splitlines()
    count_match = _TARGET_COUNT_PATTERN.match(lines[0]) if lines else None
    count = int(count_match.group("count")) if count_match is not None else None
    selected_id = ""
    for line in lines:
        if "(selected" not in line:
            continue
        target_match = _TARGET_ID_PATTERN.search(line)
        if target_match is not None:
            selected_id = target_match.group("id")
            break
    return count, selected_id


def _first_target_id(text: str) -> str:
    for line in text.splitlines():
        target_match = _TARGET_ID_PATTERN.search(line)
        if target_match is not None:
            return target_match.group("id")
    return ""


def _target_slot(target_id: str) -> str:
    """Strip only the inventory revision from a native browser target ID."""
    slot, separator, revision = target_id.rpartition(":r")
    if separator and revision and _TARGET_SLOT_PATTERN.fullmatch(slot):
        return slot
    return ""


def _target_id_for_slot(text: str, slot: str) -> str:
    if not slot:
        return ""
    for line in text.splitlines():
        target_match = _TARGET_ID_PATTERN.search(line)
        if target_match is not None and _target_slot(target_match.group("id")) == slot:
            return target_match.group("id")
    return ""


def _browser_name(text: str, target_id: str) -> str:
    """Return only the bounded browser label for one exact inventory target."""
    prefix = f"[{target_id}] "
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        browser, separator, _remainder = line[len(prefix) :].partition(" pid=")
        if separator and browser and len(browser.encode("utf-8")) <= 128:
            return browser
    return ""


def _artifact_sha256(path: Path | None) -> str:
    if path is None:
        return ""
    digest = hashlib.sha256()
    with path.resolve(strict=True).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _timed_observation(
    session: ClientSession,
    arguments: dict[str, object],
) -> tuple[dict[str, object], float, int]:
    started = time.perf_counter()
    result = await session.call_tool("observe_target", arguments)
    rendered = _tool_text(result)
    payload = _tool_json(result)
    return (
        payload,
        (time.perf_counter() - started) * 1_000,
        len(rendered.encode("utf-8")),
    )


async def _timed_expect_transaction(
    session: ClientSession,
    *,
    target_id: str,
    role: str,
    name: str,
    deadline_ms: int,
) -> tuple[dict[str, object], float, int]:
    locator: dict[str, object] = {"op": "expect", "role": role}
    if name:
        locator["name"] = name
    started = time.perf_counter()
    result = await session.call_tool(
        "execute",
        {
            "target": {"target_id": target_id},
            "steps": [locator],
            "deadline_ms": deadline_ms,
        },
    )
    rendered = _tool_text(result)
    payload = _tool_json(result, expected_status="succeeded")
    return (
        payload,
        (time.perf_counter() - started) * 1_000,
        len(rendered.encode("utf-8")),
    )


async def _run_session(
    parameters: StdioServerParameters,
    *,
    query: str,
    samples: int,
    warmups: int,
    deadline_ms: int,
    selector_role: str,
    selector_name: str,
    errlog: TextIO,
) -> dict[str, object]:
    async with stdio_client(parameters, errlog=errlog) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            before_inventory = _tool_text(await session.call_tool("list_tabs", {}))
            before_count, original_target_id = _inventory_evidence(before_inventory)
            original_target_slot = _target_slot(original_target_id)
            metadata_inventory = _tool_text(
                await session.call_tool(
                    "list_tabs",
                    {"query": query, "max_results": 1},
                )
            )
            metadata_target_id = _first_target_id(metadata_inventory)
            browser_name = _browser_name(metadata_inventory, metadata_target_id)

            query_payload, query_ms, query_output_bytes = await _timed_observation(
                session,
                {
                    "query": query,
                    "intent": "interact",
                    "max_results": 1,
                    "deadline_ms": deadline_ms,
                },
            )
            target = query_payload.get("target")
            if not isinstance(target, dict) or not isinstance(target.get("id"), str):
                raise RuntimeError("live query did not return an exact target ID")
            target_id = target["id"]

            latencies: list[float] = []
            output_sizes = [query_output_bytes]
            exact_output_sizes: list[int] = []
            cache_statuses: Counter[str] = Counter()
            for index in range(samples + warmups):
                if selector_role:
                    payload, elapsed, output_bytes = await _timed_expect_transaction(
                        session,
                        target_id=target_id,
                        role=selector_role,
                        name=selector_name,
                        deadline_ms=deadline_ms,
                    )
                    if payload.get("target_id") != target_id:
                        raise RuntimeError(
                            "live execute lost its exact target identity"
                        )
                else:
                    payload, elapsed, output_bytes = await _timed_observation(
                        session,
                        {
                            "target_id": target_id,
                            "intent": "interact",
                            "max_results": 1,
                            "deadline_ms": deadline_ms,
                        },
                    )
                    current_target = payload.get("target")
                    if not isinstance(current_target, dict) or not isinstance(
                        current_target.get("id"), str
                    ):
                        raise RuntimeError("live exact call lost its target identity")
                    target_id = current_target["id"]
                    scan = payload.get("scan")
                    if isinstance(scan, dict):
                        cache_statuses[str(scan.get("inventory_cache", "unknown"))] += 1
                exact_output_sizes.append(output_bytes)
                if not selector_role:
                    output_sizes.append(output_bytes)
                if index >= warmups:
                    latencies.append(elapsed)

            after_inventory = _tool_text(await session.call_tool("list_tabs", {}))
            after_count, selected_after_id = _inventory_evidence(after_inventory)
            selected_after_slot = _target_slot(selected_after_id)
            restored = (
                not original_target_slot
                or selected_after_slot == original_target_slot
            )
            focus_restore_calls = 0
            if original_target_slot and not restored:
                restore_target_id = _target_id_for_slot(
                    after_inventory,
                    original_target_slot,
                )
                if restore_target_id:
                    focus_restore_calls = 1
                    restore_payload, _elapsed, output_bytes = await _timed_observation(
                        session,
                        {
                            "target_id": restore_target_id,
                            "intent": "interact",
                            "max_results": 1,
                            "deadline_ms": deadline_ms,
                        },
                    )
                    output_sizes.append(output_bytes)
                    restored_target = restore_payload.get("target")
                    restored = bool(
                        isinstance(restored_target, dict)
                        and restored_target.get("id") == restore_target_id
                        and restored_target.get("activated") is True
                    )

    exact_latency = _summary(latencies)
    reference_latency_passed = bool(
        query_ms <= REFERENCE_P95_GATE_MS
        and exact_latency["median_ms"] <= REFERENCE_MEDIAN_GATE_MS
        and exact_latency["p95_ms"] <= REFERENCE_P95_GATE_MS
    )
    targets_preserved = bool(
        before_count is not None
        and after_count is not None
        and before_count == after_count
    )
    maximum_observe_output_bytes = max(output_sizes)
    maximum_exact_output_bytes = max(exact_output_sizes)
    exact_output_limit = (
        EXECUTE_OUTPUT_GATE_BYTES if selector_role else OBSERVE_TARGET_OUTPUT_GATE_BYTES
    )
    deterministic_passed = bool(
        restored
        and targets_preserved
        and browser_name
        and maximum_observe_output_bytes <= OBSERVE_TARGET_OUTPUT_GATE_BYTES
        and maximum_exact_output_bytes <= exact_output_limit
    )
    return {
        "server_version": initialized.serverInfo.version,
        "browser_name": browser_name,
        "query_resolution_ms": round(query_ms, 2),
        "exact_latency": exact_latency,
        "cache_statuses": dict(sorted(cache_statuses.items())),
        "counts": {
            "inventory_calls": 3,
            "query_resolution_calls": 1,
            "warmup_exact_calls": warmups,
            "measured_exact_calls": samples,
            "exact_execute_calls": (samples + warmups) if selector_role else 0,
            "exact_observe_calls": 0 if selector_role else (samples + warmups),
            "focus_restore_calls": focus_restore_calls,
            "shadow_mode_requests": 0,
        },
        "output": {
            "max_observe_target_bytes": maximum_observe_output_bytes,
            "max_exact_call_bytes": maximum_exact_output_bytes,
            "exact_call_limit_bytes": exact_output_limit,
        },
        "provider_phases": {
            "query_resolution_ms": round(query_ms, 2),
            "exact_call": exact_latency,
        },
        "targets_before": before_count,
        "targets_after": after_count,
        "targets_preserved": targets_preserved,
        "original_target_restored": restored,
        "gates": {
            "deterministic_passed": deterministic_passed,
            "reference_latency_passed": reference_latency_passed,
        },
    }


async def _benchmark(
    *,
    executable: Path,
    query: str,
    samples: int,
    warmups: int,
    deadline_ms: int,
    wheel: Path | None = None,
    browser_version: str = "",
    enforce_reference_latency: bool = False,
    selector_role: str = "",
    selector_name: str = "",
) -> dict[str, object]:
    if browser_version and not _BROWSER_VERSION_PATTERN.fullmatch(browser_version):
        raise ValueError("browser_version must be a compact version identifier")
    executable = executable.resolve(strict=True)
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    parameters = StdioServerParameters(
        command=str(executable),
        args=["serve"],
        cwd=Path.cwd(),
        env=environment,
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        evidence = await _run_session(
            parameters,
            query=query,
            samples=samples,
            warmups=warmups,
            deadline_ms=deadline_ms,
            selector_role=selector_role,
            selector_name=selector_name,
            errlog=errlog,
        )
    browser_name = evidence.pop("browser_name")
    gates = evidence["gates"]
    if not isinstance(gates, dict):
        raise RuntimeError("live benchmark returned invalid gate evidence")
    gates["reference_latency_enforced"] = enforce_reference_latency
    gates["passed"] = bool(
        gates["deterministic_passed"]
        and (gates["reference_latency_passed"] if enforce_reference_latency else True)
    )
    return {
        "schema_version": 1,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "executable": str(executable),
            "agent_eyes": evidence.pop("server_version"),
            "wheel_sha256": _artifact_sha256(wheel),
            "browser": {"name": browser_name, "version": browser_version},
        },
        "protocol": {
            "fixture": "live native browser through one MCP stdio session",
            "query_redacted": True,
            "warmups": warmups,
            "samples": samples,
            "deadline_ms": deadline_ms,
            "percentile": "sorted[ceil(0.95*n)-1]",
            "shadow_mode_requested": False,
            "fixed_sleeps": 0,
            "latency_gate_enforced": enforce_reference_latency,
            "exact_call": "execute_expect" if selector_role else "observe_target",
        },
        "reference_latency_gates": {
            "median_ms": REFERENCE_MEDIAN_GATE_MS,
            "p95_ms": REFERENCE_P95_GATE_MS,
        },
        **evidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--samples", type=_positive_integer, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--deadline-ms", type=_deadline, default=3_000)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--browser-version", type=_browser_version, default="")
    parser.add_argument("--enforce-reference-latency", action="store_true")
    parser.add_argument("--selector-role", default="")
    parser.add_argument("--selector-name", default="")
    arguments = parser.parse_args()
    if arguments.warmups < 0:
        parser.error("--warmups must be at least 0")
    if arguments.selector_name and not arguments.selector_role:
        parser.error("--selector-name requires --selector-role")
    result = asyncio.run(
        _benchmark(
            executable=arguments.executable,
            query=arguments.query,
            samples=arguments.samples,
            warmups=arguments.warmups,
            deadline_ms=arguments.deadline_ms,
            wheel=arguments.wheel,
            browser_version=arguments.browser_version,
            enforce_reference_latency=arguments.enforce_reference_latency,
            selector_role=arguments.selector_role,
            selector_name=arguments.selector_name,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["gates"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
