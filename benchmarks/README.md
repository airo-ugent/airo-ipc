# airo-ipc benchmarks

Cross-version benchmark scripts that let you compare the v0.2.x CycloneDDS + SHM stack with the
v1.0.0 iceoryx2 + Zenoh stack on the same machine, using equivalent workloads.

## Layout

```
benchmarks/
├── _common.py                  helpers (percentiles, json writer)
├── compare.py                  reads results/*.json and prints a side-by-side table
├── results/                    JSON output dir (gitignored)
├── new_api/
│   ├── bench_pubsub_small.py   small struct, both transports
│   ├── bench_pubsub_image.py   480x640x3 BGR image, both transports
│   └── bench_reqrep.py         request/response round-trip
└── old_api/
    ├── bench_dds_small.py      small struct over DDS
    └── bench_shm_image.py      480x640x3 BGR image over SMReader/SMWriter
```

## How to compare

The two stacks have incompatible dependencies (`cyclonedds-nightly` vs `iceoryx2 + zenoh`), so
you can't have both installed at once. Run the benchmarks in two passes against the same
working tree:

### 1. Old API (v0.2.x) — DDS + cyclone_shm

```bash
# From a clean checkout
git checkout master              # or: git checkout v0.2.1
uv sync
uv run python benchmarks/old_api/bench_dds_small.py
uv run python benchmarks/old_api/bench_shm_image.py
```

This writes `benchmarks/results/old_pubsub_small.json` and `old_pubsub_image.json`.

### 2. New API (v1.0.0) — iceoryx2 + Zenoh

```bash
# Same working tree, switch branches
git checkout v1.0.0-prep         # or whatever the rewrite branch is called
uv sync
uv run python benchmarks/new_api/bench_pubsub_small.py
uv run python benchmarks/new_api/bench_pubsub_image.py
uv run python benchmarks/new_api/bench_reqrep.py
```

This writes `new_pubsub_small.json`, `new_pubsub_image.json`, `new_reqrep.json`.

### 3. Compare

```bash
uv run python benchmarks/compare.py
```

The JSON files survive branch switches because `results/` is gitignored, so the comparison
table includes both runs.

### 4. Charts

To render the JSON into PNG charts:

```bash
uv sync --group benchmark   # one-off: installs matplotlib
uv run python benchmarks/plot.py
```

Output goes to `benchmarks/results/charts/`:

- `pubsub_small.png` — small-struct latency, per-transport horizontal bars + p95/p99/max ticks
- `pubsub_image.png` — same for the 480x640x3 image workload
- `reqrep.png` — request/response round-trip
- `overview.png` — median-only summary across all benchmarks (log scale)

## What's measured

- **Pub/sub latency**: time between the publisher's `monotonic_ns()` snapshot at send and the
  subscriber's `monotonic_ns()` snapshot at receive. Subscriber spin-polls without sleep so the
  measurement isn't contaminated by the poll cadence.
- **Image throughput**: same as above, but with a 480x640x3 uint8 buffer (≈900 KB) representing a
  webcam frame.
- **Req/rep round-trip**: time between `req.request()` call and `req.request()` return. Includes
  the requester's busy-poll cadence (default 100 µs) — that's what production code will see.

## Interpreting results

- iceoryx2 (LOCAL) should be in the tens of microseconds for small messages and one to two
  hundred microseconds for a 900 KB image (dominated by the two memcpys that copy-on-receive
  buys you for safety).
- Zenoh (NETWORK) on loopback should be hundreds of microseconds for small messages and around
  600–1000 µs for a 900 KB image (network stack + serialization).
- Old SHM (cyclone_shm) is competitive with iceoryx2 for big buffers because both ultimately
  hit the same Linux shared-memory primitives — but the iceoryx2 path is safer (no
  resource-tracker hacks, no manual buffer rotation).
- Old DDS is similar to Zenoh on loopback. The win from migrating isn't a latency cliff; it's
  smaller dependencies, more patterns (req/rep, state), and uniform version checks.

## Caveats

- All benchmarks run on a single host. Cross-machine measurements need real network hardware
  and aren't representative on loopback.
- Numbers depend heavily on CPU governor, isolation, and load. Run on an idle machine; consider
  `taskset` / `chrt` if you want stable absolute numbers.
- Sleep granularity (1 ms on most Linux systems) means the spin-poll loop is essential. Without
  it, every "no message ready" iteration would add 1 ms.
- iceoryx2 0.9 + Zenoh 1.9 are pre-1.x for iceoryx2; absolute numbers will move as the libraries
  evolve.
