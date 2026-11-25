# AGENTS.md

Guidance for AI coding agents (including Claude Code) working on this repository.

## Project

`airo-ipc` 1.0: inter-process communication for robotics in Python. The user defines **one
message class** (`airo_ipc.Message`) and selects a transport per endpoint:

- `Local()`: iceoryx2 zero-copy shared memory on one host.
- `Network(...)`: Zenoh between hosts.

Patterns: publish/subscribe and services (client/server). The 0.3 line (CycloneDDS +
`multiprocessing.shared_memory`) lives on `master` until 1.0 merges; it is not maintained here.

## Commands

```bash
uv sync --all-groups
uv run pytest                                  # ~30 s; pub/sub and service tests run on both transports
uv run pytest tests/test_pubsub.py -k "network and roundtrip"
uv run mypy airo_ipc                           # strict (mypy.ini); must pass
uv run black --check airo_ipc tests benchmarks examples
```

Always use `uv run` / `uv add`, never bare `python` or `pip`. Supported Python: 3.11+ (only
versions that are not end-of-life). CI: `.github/workflows/` (pytest matrix 3.11–3.14, mypy,
black).

## Architecture

```
airo_ipc/
  message.py      Message (ctypes.Structure) + metaclass, Array descriptor, schema fingerprint,
                  32-byte header (schema, seq, stamp_ns, reserved), with_shapes, loan guards
  transport.py    Local (+ localhost Zenoh settings for its services), Network (+ env
                  overrides), validate_topic
  endpoints.py    Publisher, Subscriber, Client, Server: all shared logic (seq numbers, loans,
                  timeouts, type checks, the lock that makes close() safe from any thread).
                  No background threads: users call receive() / spin_once() in their loop.
  context.py      Context (owns backends, endpoint tracking, pid check), default_context()
  api.py          module-level publisher/subscriber/client/server (default context)
  backends/
    base.py           driver ABCs, wait_slices(), MAX_QUEUE_DEPTH
    iox2_backend.py   iceoryx2 pub/sub drivers for Local() (companion event services for blocking
                      waits, no history, fixed sizing constants, dead-node cleanup)
    zenoh_backend.py  Zenoh drivers for Network() and for all services, also on Local()
                      (callback + Condition, liveliness tokens, queryables, exit hook)
```

Backends only move bytes; put behaviour that must be the same on both transports in
`endpoints.py`. Public API is re-exported from `airo_ipc/__init__.py`.

## Non-obvious facts (iceoryx2 0.10.1, zenoh 1.10.2)

- ctypes refuses a late `_fields_` from a Python metaclass (3.13): the metaclass builds the whole
  namespace (including `_fields_`) before class creation.
- iceoryx2 fixes service sizes at creation; openers may only ask for less. Hence fixed, generous
  sizing constants in `iox2_backend.py` (the same in every process) and a per-subscriber
  `buffer_size` for `depth`.
- iceoryx2 `listener.timed_wait(Duration 0)` blocks forever: use `_wait()` in the backend.
- iceoryx2 counts endpoints of SIGKILLed processes until `try_cleanup_dead_nodes()` runs.
- Zenoh `Handler.recv()` has no timeout; Python-callback subscribers/queryables block interpreter
  exit unless the session is closed from `threading._register_atexit` (plain `atexit` is too
  late).
- `zenoh` stubs say `Querier.matching_status` is `bool`; at runtime it has `.matching`.
- `Local()` services: Zenoh multicast scouting works on the loopback interface, also in a Docker
  container (Linux `lo` has no MULTICAST flag, but it works; verified). The interface is given as
  the IP `127.0.0.1`. A distinct scouting port (7448) keeps `Network()` sessions away.

## Conventions

- All code under `airo_ipc/` is fully typed and black-formatted.
- New behaviour gets a test that runs on both transports (`pair` fixture in `tests/conftest.py`).
- Error messages say what happened and what to do.
- Multiprocessing: spawn context only; share only spawn-context primitives.
