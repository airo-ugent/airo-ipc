# airo-ipc benchmarks

Latency benchmarks for airo-ipc 1.0 (iceoryx2 + Zenoh). Every benchmark runs the publisher (or
server) in a spawned process.

```
benchmarks/
├── _common.py                  percentiles, JSON output, transport pairs
├── results/                    JSON output (gitignored)
├── bench_pubsub_small.py       small message, Local() and Network()
├── bench_pubsub_image.py       480x640x3 image: receive() copy, receive_loan(), spin-polling
└── bench_reqrep.py             service round trip
```

`Network()` runs over loopback TCP between two Zenoh sessions, with multicast off.

## Run

```bash
uv sync --all-groups
uv run python benchmarks/bench_pubsub_small.py
uv run python benchmarks/bench_pubsub_image.py
uv run python benchmarks/bench_reqrep.py
```

Each script prints a summary and writes JSON to `benchmarks/results/`.

## Results

The 0.3 numbers were measured once with the 0.3 benchmark scripts (removed after the comparison;
see the git history of `benchmarks/old_api/` to run them again).

Results on one Linux x86-64 machine (median / p95, µs):

| Workload | 0.3 | 1.0 `Local()` | 1.0 `Network()` |
|---|---|---|---|
| Small message | 42 / 140 (spin-polling) | 40 / 177 | 97 / 371 |
| Image, `receive()` copy | — | 283 / 431 | 747 / 1233 |
| Image, `receive_loan()` | 186 / 237 (spin-polling) | 157 / 283 | 672 / 976 |
| Image, `receive_loan()` spin-polling | — | 22 / 30 | (do not poll on Network) |
| Service round trip | — | 80 / 110 (Zenoh on loopback; 27 / 33 with the former iceoryx2 services) | 81 / 122 |

Notes:

- The 0.3 subscribers poll in a busy loop (one CPU core at 100 %). The 1.0 subscribers block
  until an event arrives, except in the "spin-polling" row.
- Most of the blocking 1.0 latency is the operating system waking the subscriber process.
- Spin-polling on `Network()` is slow (4 ms) because the polling loop and Zenoh's receive thread
  compete for the GIL.
