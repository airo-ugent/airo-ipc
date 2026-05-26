"""Print a side-by-side comparison of new-API and old-API benchmark results.

Reads JSON files in benchmarks/results/ that the benchmark scripts produce.
"""

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"


def load(name: str) -> dict | None:
    f = RESULTS_DIR / f"{name}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def fmt(summary: dict | None, key: str) -> str:
    if summary is None or summary.get("n", 0) == 0:
        return "      —"
    return f"{summary[key]:8.1f}"


def section(title: str) -> None:
    print(f"\n=== {title} ===")
    print(
        f"  {'transport':<22}"
        f"{'median':>10}"
        f"{'p95':>10}"
        f"{'p99':>10}"
        f"{'max':>10}  ({'n':>4})"
    )
    print(f"  {'-' * 22}{'-' * 10}{'-' * 10}{'-' * 10}{'-' * 10}  {'-' * 6}")


def row(label: str, summary: dict | None) -> None:
    if summary is None or summary.get("n", 0) == 0:
        print(f"  {label:<22}{'no data':>10}")
        return
    print(
        f"  {label:<22}"
        f"{fmt(summary, 'median')}{fmt(summary, 'p95')}{fmt(summary, 'p99')}{fmt(summary, 'max')}"
        f"  ({summary['n']:>4})"
    )


def compare_pubsub_small() -> None:
    old = load("old_pubsub_small")
    new = load("new_pubsub_small")
    if old is None and new is None:
        return
    section("Small-message pub/sub latency (us)")
    if old is not None:
        row("OLD / DDS (network)", old["results"].get("network"))
    if new is not None:
        row("NEW / LOCAL (iox2)", new["results"].get("local"))
        row("NEW / NETWORK (zenoh)", new["results"].get("network"))


def compare_pubsub_image() -> None:
    old = load("old_pubsub_image")
    new = load("new_pubsub_image")
    if old is None and new is None:
        return
    section("480x640x3 BGR image pub/sub latency (us)")
    if old is not None:
        row("OLD / SHM (local)", old["results"].get("local"))
    if new is not None:
        row("NEW / LOCAL (iox2)", new["results"].get("local"))
        row("NEW / NETWORK (zenoh)", new["results"].get("network"))


def compare_reqrep() -> None:
    new = load("new_reqrep")
    if new is None:
        return
    section("Request/response round-trip latency (us)")
    print("  (no equivalent in v0.2.x — patterns were pub/sub only)")
    row("NEW / LOCAL (iox2)", new["results"].get("local"))
    row("NEW / NETWORK (zenoh)", new["results"].get("network"))


def main() -> int:
    print("airo-ipc benchmark comparison")
    print("results dir:", RESULTS_DIR)
    if not any(RESULTS_DIR.glob("*.json")):
        print("\nNo results found. Run the benchmarks first; see benchmarks/README.md.")
        return 1

    compare_pubsub_small()
    compare_pubsub_image()
    compare_reqrep()
    return 0


if __name__ == "__main__":
    sys.exit(main())
