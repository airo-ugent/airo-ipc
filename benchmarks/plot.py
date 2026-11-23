"""Render benchmark JSON results in benchmarks/results/ to PNG charts.

Produces:
  results/charts/pubsub_small.png   (small struct latency)
  results/charts/pubsub_image.png   (480x640x3 image latency)
  results/charts/reqrep.png         (req/rep round-trip)
  results/charts/overview.png       (all transports, log-scale, median)
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = Path(__file__).parent / "results"
CHARTS_DIR = RESULTS_DIR / "charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)


# Consistent color per transport across all charts.
COLOR = {
    "OLD / DDS (network)": "#a0522d",  # sienna — old network
    "OLD / SHM (local)": "#cd853f",  # peru — old local
    "NEW / LOCAL (iox2)": "#1f77b4",  # blue — new local
    "NEW / NETWORK (zenoh)": "#ff7f0e",  # orange — new network
}


def _load(name: str) -> dict | None:
    f = RESULTS_DIR / f"{name}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def _rows_for(name: str) -> list[tuple[str, dict]]:
    """Return [(label, summary), ...] for a given benchmark, ordered for plotting."""
    rows: list[tuple[str, dict]] = []
    old = _load(f"old_{name}")
    new = _load(f"new_{name}")
    if old is not None:
        if "local" in old.get("results", {}):
            rows.append(("OLD / SHM (local)", old["results"]["local"]))
        if "network" in old.get("results", {}):
            rows.append(("OLD / DDS (network)", old["results"]["network"]))
    if new is not None:
        results = new.get("results", {})
        # The image benchmark reports several read modes; plot the zero-copy read on Local()
        # and the copy on Network() (both blocking, no spin-polling).
        local = results.get("local") or results.get("local_loan")
        network = results.get("network") or results.get("network_copy")
        if local is not None:
            rows.append(("NEW / LOCAL (iox2)", local))
        if network is not None:
            rows.append(("NEW / NETWORK (zenoh)", network))
    return rows


def plot_benchmark(name: str, title: str, subtitle: str = "") -> Path | None:
    """Horizontal latency chart per benchmark: bars = median, whiskers = p95/p99/max."""
    rows = _rows_for(name)
    if not rows:
        return None

    labels = [r[0] for r in rows]
    medians = [r[1]["median"] for r in rows]
    p95s = [r[1]["p95"] for r in rows]
    p99s = [r[1]["p99"] for r in rows]
    maxs = [r[1]["max"] for r in rows]
    ns = [r[1]["n"] for r in rows]
    colors = [COLOR.get(lab, "#666") for lab in labels]

    fig, ax = plt.subplots(figsize=(9, 1.8 + 0.7 * len(rows)))
    ypos = np.arange(len(rows))

    # Bars for median latency.
    bars = ax.barh(ypos, medians, color=colors, alpha=0.85, label="median")

    # Markers for p95 / p99 / max as tick lines at increasing heights.
    for i, (m, p95, p99, mx) in enumerate(zip(medians, p95s, p99s, maxs)):
        # Min-to-max range line behind the bar.
        ax.plot([m, mx], [i, i], color="#888", lw=1.0, zorder=1)
        # p95 / p99 / max tick markers.
        ax.plot(p95, i, marker="|", markersize=14, color="#444", markeredgewidth=2)
        ax.plot(p99, i, marker="|", markersize=14, color="#c33", markeredgewidth=2)
        ax.plot(mx, i, marker="|", markersize=10, color="#000", markeredgewidth=1.5)

    # Annotate the median value at the end of each bar.
    for i, (m, n) in enumerate(zip(medians, ns)):
        ax.text(m, i, f"  {m:.1f} µs  (n={n})", va="center", fontsize=9, color="#333")

    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("latency (µs)  —  log scale")
    ax.set_xscale("log")
    full_title = title if not subtitle else f"{title}\n{subtitle}"
    ax.set_title(full_title, fontsize=12, weight="bold", loc="left")

    # Legend for the marker scheme (bar color is per-row, so we use neutral grey for the median entry).
    legend_elements = [
        plt.Line2D([0], [0], color="#888", lw=8, label="median (bar)"),
        plt.Line2D(
            [0], [0], marker="|", color="#444", markersize=14, lw=0, label="p95"
        ),
        plt.Line2D(
            [0], [0], marker="|", color="#c33", markersize=14, lw=0, label="p99"
        ),
        plt.Line2D(
            [0], [0], marker="|", color="#000", markersize=10, lw=0, label="max"
        ),
    ]
    ax.legend(handles=legend_elements, loc="lower right", fontsize=8, framealpha=0.9)

    ax.grid(axis="x", which="both", alpha=0.3)
    ax.set_axisbelow(True)
    plt.tight_layout()
    out = CHARTS_DIR / f"{name}.png"
    plt.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_overview() -> Path | None:
    """One chart with median latency for every benchmark/transport combo."""
    benchmarks = [
        ("pubsub_small", "small struct"),
        ("pubsub_image", "480x640x3 image"),
        ("reqrep", "req/rep round-trip"),
    ]

    data: dict[str, list[tuple[str, float]]] = {}
    for bench_name, _ in benchmarks:
        rows = _rows_for(bench_name)
        if not rows:
            continue
        data[bench_name] = [(label, r["median"]) for label, r in rows]

    if not data:
        return None

    # Group bars by benchmark; each benchmark gets a cluster of transport bars.
    all_labels = []
    for bench_name in data:
        for label, _ in data[bench_name]:
            if label not in all_labels:
                all_labels.append(label)

    fig, ax = plt.subplots(figsize=(10, 5))
    bar_width = 0.18
    bench_centers = np.arange(len(data))
    offset_map = {
        lab: (i - (len(all_labels) - 1) / 2) * bar_width
        for i, lab in enumerate(all_labels)
    }

    for label in all_labels:
        ys = []
        xs = []
        for i, (bench_name, _) in enumerate(benchmarks):
            if bench_name not in data:
                continue
            d = dict(data[bench_name])
            if label in d:
                ys.append(d[label])
                xs.append(
                    bench_centers[list(data).index(bench_name)] + offset_map[label]
                )
        if ys:
            ax.bar(xs, ys, width=bar_width, color=COLOR.get(label, "#666"), label=label)

    ax.set_xticks(bench_centers)
    ax.set_xticklabels([bn for bn in data])
    ax.set_yscale("log")
    ax.set_ylabel("median latency (µs)  —  log scale")
    ax.set_title(
        "airo-ipc benchmark overview — median latency per transport",
        fontsize=12,
        weight="bold",
    )
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(axis="y", which="both", alpha=0.3)
    ax.set_axisbelow(True)
    plt.tight_layout()
    out = CHARTS_DIR / "overview.png"
    plt.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main() -> None:
    print(f"Reading results from {RESULTS_DIR}")
    paths = []
    p = plot_benchmark(
        "pubsub_small",
        "Small-struct pub/sub latency",
        "32-byte TimedPoint; subscriber spin-polls; lower is better",
    )
    if p:
        paths.append(p)
    p = plot_benchmark(
        "pubsub_image",
        "480×640×3 BGR image pub/sub latency",
        "~900 KB payload; subscriber spin-polls; lower is better",
    )
    if p:
        paths.append(p)
    p = plot_benchmark(
        "reqrep",
        "Request/response round-trip latency",
        "client-perceived round trip; includes server processing; lower is better",
    )
    if p:
        paths.append(p)
    p = plot_overview()
    if p:
        paths.append(p)

    print("\nWrote charts:")
    for p in paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()
