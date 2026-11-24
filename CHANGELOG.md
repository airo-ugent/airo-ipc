# Changelog

## 1.0.0 (unreleased)

A rewrite on iceoryx2 (local) and Zenoh (network). The API is not compatible with 0.3; see
[MIGRATION.md](MIGRATION.md).

### Added

- `Message`: one class definition for every transport, with `Array(shape, dtype)` numpy fields,
  sized scalar aliases, runtime shapes (`with_shapes`), and an automatic schema fingerprint that
  turns definition mismatches into `SchemaMismatchError`.
- Transports `Local()` (iceoryx2 zero-copy shared memory for pub/sub; Zenoh on localhost for
  services, no configuration) and `Network(connect, listen, namespace, multicast, mode,
  config)` (Zenoh), with `AIRO_IPC_CONNECT`, `AIRO_IPC_LISTEN` and
  `AIRO_IPC_NAMESPACE` environment variables.
- Publish/subscribe: `publish`, zero-copy `loan()` and `receive_loan()` with guards against use
  after release, `receive(timeout)` without polling, a `depth` argument for the receive queue,
  `has_subscribers` / `has_publishers`.
- Services: `server(...)` with `spin_once()` in your own loop, `client(...).call()` /
  `wait_for_server()`, remote exceptions as `ServiceError`, `NoServerError`.
- No background threads: endpoints are used from one thread, and `close()` is safe from any
  thread.
- `Context` with explicit lifetime, and a per-process default context.
- Benchmarks against 0.3, and tests that run every case on both transports.

### Removed

- CycloneDDS, `BaseIdl`, `IdlStruct` messages, `SMReader` / `SMWriter`, `DDSReader` /
  `DDSWriter`, the `airo_ipc.framework` package (`Node`), `IpcKind`, and the loguru dependency.
  Write a plain process loop instead of a `Node`; see [MIGRATION.md](MIGRATION.md).
- iceoryx2 request/response: services use Zenoh on both transports (on localhost for
  `Local()`), so they have one implementation.
- Latched topics: a subscriber receives only messages published after it connected. Serve
  startup state (a resolution, a configuration) with a service instead.
- Python 3.9 and 3.10 support (end of life).

## 0.3.0

Last release of the CycloneDDS + `multiprocessing.shared_memory` stack.
