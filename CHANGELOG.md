# Changelog

## 1.0.0 (unreleased)

A rewrite on iceoryx2 (local) and Zenoh (network). The API is not compatible with 0.3; see
[MIGRATION.md](MIGRATION.md).

### Added

- `Message`: one class definition for every transport, with `Array(shape, dtype)` numpy fields,
  sized scalar aliases, runtime shapes (`with_shapes`), and an automatic schema fingerprint that
  turns definition mismatches into `SchemaMismatchError`.
- Transports `Local()` (iceoryx2 zero-copy shared memory) and `Network(connect, listen,
  namespace, multicast, mode, config)` (Zenoh), with `AIRO_IPC_CONNECT`, `AIRO_IPC_LISTEN` and
  `AIRO_IPC_NAMESPACE` environment variables.
- Publish/subscribe: `publish`, zero-copy `loan()` and `receive_loan()` with guards against use
  after release, `receive(timeout)` without polling, `on_message`, `Latest()` / `Queue(n)` QoS,
  latched topics, `has_subscribers` / `has_publishers`.
- Services: `server(...)` (background thread or `spin_once`), `client(...).call()` /
  `call_async()` / `wait_for_server()`, remote exceptions as `ServiceError`, `NoServerError`.
- `Context` with explicit lifetime, and a per-process default context.
- `Worker` (successor of `Node`) with service support.
- Benchmarks against 0.3, and tests that run every case on both transports.

### Removed

- CycloneDDS, `BaseIdl`, `IdlStruct` messages, `SMReader` / `SMWriter`, `DDSReader` /
  `DDSWriter`, `Node`, `IpcKind`, and the loguru dependency.
- Python 3.9 and 3.10 support (end of life).

## 0.3.0

Last release of the CycloneDDS + `multiprocessing.shared_memory` stack.
