# airo-ipc benchmarks

Latency benchmarks for airo-ipc 1.0 (iceoryx2 + Zenoh), and the same workloads on 0.3
(CycloneDDS + `multiprocessing.shared_memory`) for comparison. Every benchmark runs the
publisher (or server) in a spawned process.

```
benchmarks/
├── _common.py                  percentiles, JSON output, transport pairs
├── compare.py                  prints a side-by-side table from results/*.json
├── plot.py                     plots from results/*.json
├── results/                    JSON output (gitignored)
├── new_api/                    1.0
│   ├── bench_pubsub_small.py   small message, Local() and Network()
│   ├── bench_pubsub_image.py   480x640x3 image: receive() copy, receive_loan(), spin-polling
│   └── bench_reqrep.py         service round trip
└── old_api/                    0.3
    ├── bench_dds_small.py      small message over DDS
    └── bench_shm_image.py      480x640x3 image over SMReader/SMWriter
```

`Network()` runs over loopback TCP between two Zenoh sessions, with multicast off.

## Run 1.0

```bash
uv sync --all-groups
uv run python benchmarks/new_api/bench_pubsub_small.py
uv run python benchmarks/new_api/bench_pubsub_image.py
uv run python benchmarks/new_api/bench_reqrep.py
```

## Run 0.3

The two versions have different dependencies. Run the old scripts from this branch with the
environment of a `master` worktree:

```bash
git worktree add /tmp/airo-ipc-0.3 master
cd /tmp/airo-ipc-0.3 && uv sync
uv run python <this-repo>/benchmarks/old_api/bench_dds_small.py
uv run python <this-repo>/benchmarks/old_api/bench_shm_image.py
cd - && git worktree remove /tmp/airo-ipc-0.3
```

The results go to `<this-repo>/benchmarks/results/`. The 0.3 scripts print `KeyError` lines at
exit; that is a known 0.3 resource-tracker problem and does not affect the numbers.

## Compare

```bash
uv run python benchmarks/compare.py
```

Results on one Linux x86-64 machine (median / p95, µs):

| Workload | 0.3 | 1.0 `Local()` | 1.0 `Network()` |
|---|---|---|---|
| Small message | 42 / 140 (spin-polling) | 40 / 177 | 97 / 371 |
| Image, `receive()` copy | — | 283 / 431 | 747 / 1233 |
| Image, `receive_loan()` | 186 / 237 (spin-polling) | 157 / 283 | 672 / 976 |
| Image, `receive_loan()` spin-polling | — | 22 / 30 | (do not poll on Network) |
| Service round trip | — | 27 / 33 | 81 / 122 |

Notes:

- The 0.3 subscribers poll in a busy loop (one CPU core at 100 %). The 1.0 subscribers block
  until an event arrives, except in the "spin-polling" row.
- Most of the blocking 1.0 latency is the operating system waking the subscriber process.
- Spin-polling on `Network()` is slow (4 ms) because the polling loop and Zenoh's receive thread
  compete for the GIL.
