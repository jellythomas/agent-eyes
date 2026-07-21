"""Repeatable CDP legacy-enrichment round-trip benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from agent_eyes.adapters.base import UIElement
from agent_eyes.cdp import CDPClient


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


async def _run(elements: int, round_trip_ms: float) -> dict[str, float | int]:
    client = CDPClient()
    root = UIElement(id=0, role="document")
    root.children = [
        UIElement(id=index, role="button", platform_ref=index)
        for index in range(1, elements + 1)
    ]
    calls: list[str] = []

    async def send(_ws, method: str, _params=None, **_kwargs):
        calls.append(method)
        if round_trip_ms:
            await asyncio.sleep(round_trip_ms / 1_000)
        if method == "DOM.getBoxModel":
            return {"model": {"border": [0, 0, 10, 0, 10, 10, 0, 10]}}
        return {}

    async def prior_serial_enrichment() -> None:
        await send(object(), "DOM.enable")
        await send(object(), "CSS.enable")
        for _index in range(elements):
            await send(object(), "DOM.getBoxModel")
            await send(object(), "DOM.describeNode")
            await send(object(), "CSS.getComputedStyleForNode")

    started = time.perf_counter()
    await prior_serial_enrichment()
    previous_elapsed_ms = (time.perf_counter() - started) * 1_000
    previous_round_trips = len(calls)

    calls.clear()
    client._send = send
    started = time.perf_counter()
    enriched = await client._enrich_tree(object(), root, limit=elements)
    current_elapsed_ms = (time.perf_counter() - started) * 1_000

    current_round_trips = len(calls)
    return {
        "elements": elements,
        "enriched": enriched,
        "simulated_round_trip_ms": round_trip_ms,
        "previous_serial_round_trips": previous_round_trips,
        "current_serial_round_trips": current_round_trips,
        "round_trip_reduction_percent": round(
            100 * (previous_round_trips - current_round_trips) / previous_round_trips,
            2,
        ),
        "previous_measured_elapsed_ms": round(previous_elapsed_ms, 2),
        "current_measured_elapsed_ms": round(current_elapsed_ms, 2),
        "measured_speedup": round(previous_elapsed_ms / current_elapsed_ms, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elements", type=_positive_integer, default=60)
    parser.add_argument(
        "--round-trip-ms",
        type=_non_negative_float,
        default=1.0,
        help="Simulated serialized CDP command latency.",
    )
    arguments = parser.parse_args()
    result = asyncio.run(_run(arguments.elements, arguments.round_trip_ms))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
