"""Helpers shared between the new-API and old-API benchmarks."""

import json
import os
import statistics
from pathlib import Path
from typing import Iterable

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def percentile(data: list, p: float) -> float:
    if not data:
        return float("nan")
    s = sorted(data)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def summarize(samples: list, unit: str = "us") -> dict:
    if not samples:
        return {"unit": unit, "n": 0}
    return {
        "unit": unit,
        "n": len(samples),
        "mean": statistics.mean(samples),
        "median": statistics.median(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "min": min(samples),
        "max": max(samples),
    }


def write_result(name: str, payload: dict) -> Path:
    out = RESULTS_DIR / f"{name}.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return out


def print_summary(name: str, summary: dict) -> None:
    unit = summary.get("unit", "")
    print(f"\n=== {name} ===")
    if summary.get("n", 0) == 0:
        print("  no samples")
        return
    print(f"  n      = {summary['n']}")
    print(f"  mean   = {summary['mean']:.2f} {unit}")
    print(f"  median = {summary['median']:.2f} {unit}")
    print(f"  p95    = {summary['p95']:.2f} {unit}")
    print(f"  p99    = {summary['p99']:.2f} {unit}")
    print(f"  stdev  = {summary['stdev']:.2f} {unit}")
    print(f"  min    = {summary['min']:.2f} {unit}")
    print(f"  max    = {summary['max']:.2f} {unit}")


def transport_pairs() -> dict:
    """(publisher-side, subscriber-side) transports for the new API.

    Network uses two Zenoh sessions over loopback TCP with multicast off, like the tests.
    """
    import socket
    import uuid

    import airo_ipc as ipc

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    namespace = f"bench{uuid.uuid4().hex[:8]}"
    return {
        "local": (ipc.Local(), ipc.Local()),
        "network": (
            ipc.Network(
                listen=f"tcp/127.0.0.1:{port}", multicast=False, namespace=namespace
            ),
            ipc.Network(
                connect=f"tcp/127.0.0.1:{port}", multicast=False, namespace=namespace
            ),
        ),
    }
