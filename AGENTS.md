# AGENTS.md

This file provides guidance to AI coding agents (including Claude Code) when working with code in this repository.

## Project

`airo-ipc` is a Python library for inter-process communication, combining **shared-memory** transport (for large numpy arrays, zero-copy) with **Cyclone DDS** (for synchronization and for small/structured messages). It originated to simplify multiprocessing in `airo-camera-toolkit` (high-bandwidth camera frames). The README notes that for new projects iceoryx2/Zenoh may be better choices — keep this framing in mind, but the library is still actively maintained for research use.

## Commands

The project uses **uv**. Install the package with all dev/benchmark groups before working:

```bash
uv sync --all-groups
```

- Run tests: `uv run pytest`
- Run a single test: `uv run pytest tests/test_shared_memory_roundtrip.py::test_shared_memory_roundtrip_2d_array_content`
- Type-check (must pass CI): `uv run mypy airo_ipc`
- Format check / apply: `uv run black --check airo_ipc` / `uv run black airo_ipc`

CI (GitHub Actions) runs these three as separate workflows on push/PR to `master`. mypy and black target only the `airo_ipc/` package; mypy config in `mypy.ini` is strict (`disallow_untyped_defs`, `disallow_any_unimported`, `no_implicit_optional`). All new code under `airo_ipc/` must be fully typed and black-formatted.

Run the benchmark (airo-ipc vs Zenoh RGB-D pub/sub) from the repo root:
```bash
python -m benchmarks.run_benchmark --backend both --resolution all --mode both
```

## Architecture

Two layers, used independently or together:

### 1. `airo_ipc/cyclone_shm/` — the core transport

A single-writer / multiple-reader shared-memory channel synchronized over DDS. The data contract is a dataclass extending **`BaseIdl`** (`idl_shared_memory/base_idl.py`) whose fields are all numpy arrays with fixed, pre-known shapes and dtypes (`get_fields()` enforces numpy-only). `BaseIDL` is a deprecated alias.

- **`SMWriter` / `SMReader`** (`patterns/sm_writer.py`, `patterns/sm_reader.py`): For each field, `nr_of_buffers` (default 3) shared-memory segments are allocated, named `"{topic}.{field}.buffer_{i}"`. On each `writer(msg)` call the writer copies field data into the next buffer (round-robin) and publishes the buffer index + timestamp over a DDS sync topic named `"{topic}__buffer_nr"` (`BufferNrSample` IDL). The reader subscribes to that topic, then reads the corresponding shared-memory buffer. The buffer rotation is what reduces (but does not eliminate) read/write data races — raise `nr_of_buffers` to lower race probability; the value **must match between writer and reader**.
- **Zero-copy reads**: `SMReader.__call__` / `read_into` return numpy views directly into shared memory — *no copy is made*. Callers must deep-copy immediately if they need data to survive the next write. This is intentional (speed over consistency).
- **`DDSReader` / `DDSWriter`** (`patterns/ddsreader.py`, `patterns/ddswriter.py`): thin wrappers over Cyclone DDS for normal (non-shared-memory) messages, which must be `cyclonedds.idl.IdlStruct` dataclasses. Default QoS (`defaults.py`) is BestEffort + KeepLast(1). `read()` returns latest sample without consuming; `take()` consumes.
- **Resource-tracker workaround** (`SharedMemoryNoResourceTracker` in `sm_reader.py`): readers unregister segments from Python's `multiprocessing.resource_tracker` so a reader exiting does not unlink memory still owned by the writer. The writer owns creation/unlinking; the writer's `SMWriter` also recovers from stale segments left by a killed process (`FileExistsError` → unlink + recreate).

### 2. `airo_ipc/framework/` — optional high-level node abstraction

**`Node`** (`framework/node.py`) is a `multiprocessing.SpawnProcess` + ABC implementing a publish/subscribe loop. Subclasses implement `_setup`, `_step`, `_teardown`; the base `run()` loop calls `_update_subscriptions()` (fires registered callbacks) then `_step()` at a target `update_frequency`. Use `_register_publisher` / `_publish` and `_subscribe`, each taking an **`IpcKind`** (`DDS` or `SHARED_MEMORY`, from `framework/framework.py`) that selects the underlying reader/writer pair. DDS topics expect an `IdlMeta` IDL; shared-memory topics expect a `BaseIdl` instance. Teardown order matters: SM readers/writers are stopped before the `DomainParticipant` is closed.

**Critical gotcha — spawn context**: `Node` always starts via the "spawn" method. Any `multiprocessing` primitive (`Event`, `Value`, `Queue`, `Lock`, ...) shared with a Node must be created from the spawn context (`multiprocessing.get_context("spawn").Event()`), not bare `multiprocessing.Event()`, which uses the platform default (fork on Linux) and causes silent failures. See issues #5 and #7.

## Import paths

Use full module paths as in `examples/` and `tests/` (e.g. `from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter`, `from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl`). The package `__init__.py` files are empty, so the short imports shown in some README snippets (`from airo_ipc.cyclone_shm import SMWriter`) do **not** work as written.

## Examples & tests

`examples/framework/` (`webcam.py`, `airo_mono.py`) are the best end-to-end references — `webcam.py` shows publishing DDS (resolution) and shared-memory (frames) from the same Node, and subscribing to a shared-memory topic only after its shape is known. `tests/` covers the shared-memory roundtrip; the pytest CI step tolerates exit code 5 (no tests collected).
