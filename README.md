# airo-ipc

Fast inter-process communication for robotics in Python. You write **one message class** and pick
a transport per endpoint:

- **`Local()`**: between processes on one host. Publish/subscribe uses
  [iceoryx2](https://iceoryx2.org/) zero-copy shared memory; services use Zenoh on localhost
  (see [Services on `Local()`](#services-on-local)).
- **`Network(...)`**: [Zenoh](https://zenoh.io/) between hosts (and between processes on one
  host).

Publish/subscribe and services (client/server) work the same on both transports.

```python
import numpy as np
from numpy.typing import NDArray
import airo_ipc as ipc


class CameraFrame(ipc.Message):
    stamp: float
    rgb: NDArray[np.uint8] = ipc.Array((480, 640, 3), np.uint8)


# Process A
pub = ipc.publisher("camera/front", CameraFrame, ipc.Local())
with pub.loan() as frame:          # write straight into shared memory
    frame.stamp = 0.5
    frame.rgb[:] = image

# Process B (same host) - or use ipc.Network(...) on both sides to go between hosts
sub = ipc.subscriber("camera/front", CameraFrame, ipc.Local())
frame = sub.receive(timeout=1.0)   # the newest frame, or None after 1 s
```

## Installation

```bash
uv add airo-ipc        # or: pip install airo-ipc
```

Python 3.11 or newer. Tested on Linux (x86-64). Both backends ship as Python wheels: there is no
daemon to run and nothing to compile.

## Choosing a transport

The transport is a required argument of every factory, so the code always shows where the data
goes. The topic name and the message class are the same for both.

| | `Local()` | `Network(...)` |
|---|---|---|
| Library | iceoryx2 (pub/sub), Zenoh on localhost (services) | Zenoh |
| Scope | processes on one host | hosts on a network (also works on one host) |
| Copies | zero-copy with `loan()` / `receive_loan()` | serialized over TCP/UDP (raw bytes, no encoding) |
| Typical latency (900 KB image) | 20–160 µs | 0.7 ms on loopback |

`Network()` needs no IP address on a LAN with multicast: Zenoh peers find each other. Give an
endpoint when multicast is blocked (common on university and corporate Wi-Fi), across subnets,
or through a Zenoh router:

```python
ipc.Network()                                  # multicast discovery
ipc.Network(connect="192.168.1.20")            # = "tcp/192.168.1.20:7447"
ipc.Network(listen="0.0.0.0")                  # accept connections on port 7447
ipc.Network(connect="10.0.0.2", namespace="cell_2")   # isolate two setups on one network
ipc.Network(config="zenoh.json5")              # a full Zenoh configuration file
```

The environment variables `AIRO_IPC_CONNECT`, `AIRO_IPC_LISTEN` and `AIRO_IPC_NAMESPACE` fill in
what the code leaves empty. The code can then say `Network()`, and each deployment supplies its
own addresses.

## Messages

A message is a fixed-size `ctypes.Structure` that you define with annotations:

```python
class RobotState(ipc.Message):
    stamp: float                      # 64-bit float
    mode: ipc.u8 = 0                  # sized scalars: i8 i16 i32 i64 u8 u16 u32 u64 f32 f64
    enabled: bool = False
    joints: NDArray[np.float64] = ipc.Array((6,), np.float64)
```

- Fields: `float`, `int` (64-bit), `bool`, the sized aliases, and fixed-shape numpy arrays with
  `Array(shape, dtype)` (any bool, integer, float or complex dtype).
- Array fields are numpy views into the message memory: `msg.joints[2] = 0.1` writes in place.
  `msg.joints = values` copies `values` in (the shape must match).
- Construct with keywords: `RobotState(stamp=1.0, joints=q)`. Unknown fields raise an error,
  also on assignment (`msg.stmap = 1` fails).
- `msg.meta.seq` and `msg.meta.stamp_ns` hold the sequence number and publish time that the
  publisher wrote.

**Runtime shapes.** When a shape is only known at runtime, for example a camera resolution, write
`None` for the unknown dimensions and create a concrete class with `with_shapes`:

```python
class CameraFrame(ipc.Message):
    rgb: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)

Frame = CameraFrame.with_shapes(rgb=(720, 1280, 3))   # same call -> same class
```

**Schema check.** Every message type has a 64-bit fingerprint of its class name and of every field
name, type and shape. A publisher and a subscriber with different definitions fail loudly with
`SchemaMismatchError` (on `Local()` when the endpoint opens, on `Network()` on the first
message) instead of silently reading garbage. There is no version number to maintain.

## Publish / subscribe

```python
pub = ipc.publisher("robot/state", RobotState, transport)
pub.publish(RobotState(stamp=time.time()))

sub = ipc.subscriber("robot/state", RobotState, transport)
msg = sub.receive()               # does not wait: a message or None
msg = sub.receive(timeout=1.0)    # waits up to 1 s (event-based, no polling)
msg = sub.receive(timeout=None)   # waits until a message arrives
```

**Queue depth.** `ipc.subscriber(..., depth=1)` (the default) keeps only the newest unread
message: right for sensor streams. `depth=n` keeps up to `n` messages in order (at most
`ipc.MAX_QUEUE_DEPTH`). The publisher never blocks; when the queue is full, the oldest message is
dropped.

**Late joiners.** A subscriber receives only messages published after it connected. For state
that a process needs at startup, such as a resolution or a configuration, use a service: the
owner serves the current value, and a process that starts later asks for it (see below).

**Discovery.** `pub.has_subscribers` and `sub.has_publishers` tell whether the other side is
connected.

### Zero-copy loans

On `Local()`, `receive()` copies the message out of shared memory, so you own the result. To avoid
copies of large messages, write and read in place:

```python
with pub.loan() as frame:              # memory that only this publisher holds
    camera.read_into(frame.rgb)        # the only copy: from the camera into shared memory
    frame.stamp = time.time()
# sent here; if the block raises, nothing is sent

with sub.receive_loan(timeout=1.0) as frame:   # read-only view into shared memory
    if frame is not None:
        detections = model(frame.rgb)
```

Why the data cannot change under you: iceoryx2 counts the holders of every shared-memory chunk
and reuses a chunk only when no publisher, subscriber queue or reader holds it. A new `loan()`
always gets a free chunk, never one that a reader still holds. (airo-ipc 0.3 used a fixed ring
of three buffers and could overwrite a slow reader's data.)

Rules that keep this safe in Python:

- After the `with` block, field access raises `LoanReleasedError`, and numpy views from the block
  become read-only. Copy what you need inside the block (`frame.copy()` or `np.copy(...)`).
- A received loan is read-only (`ReadOnlyMessageError` on writes).
- A numpy view that you keep after a `receive_loan()` block keeps its chunk borrowed until it is
  garbage-collected (airo-ipc logs a warning once). A subscriber may hold only a few borrowed
  chunks at the same time (`TooManyLoansError` after that).

On `Network()` the same code works; Zenoh copies the bytes anyway.

## Services

```python
class MoveRequest(ipc.Message):
    target: NDArray[np.float64] = ipc.Array((6,), np.float64)
    speed: float

class MoveResponse(ipc.Message):
    success: bool
    duration: float

def handle(request: MoveRequest) -> MoveResponse:
    return MoveResponse(success=True, duration=robot.move(request.target, request.speed))

# Server process: handle requests in your own loop.
server = ipc.server("ur5e/move", MoveRequest, MoveResponse, transport, handle)
while running:
    server.spin_once(timeout=0.1)   # handles at most one request

# Client process
client = ipc.client("ur5e/move", MoveRequest, MoveResponse, transport)
client.wait_for_server(timeout=10.0)
response = client.call(MoveRequest(target=q, speed=0.2), timeout=5.0)
```

- If the handler raises, `call()` raises `ServiceError` with the remote error text.
- With no server, `call()` raises `NoServerError` at once; a slow server gives `TimeoutError`.
- If two servers serve the same service, the client uses the first reply.

### Services on `Local()`

On `Local()`, services use Zenoh instead of iceoryx2, and need no configuration. The traffic stays
on the host: each process listens on a random port on 127.0.0.1, and the processes find each
other with multicast on the loopback interface. That discovery uses its own port and key
namespace, so `Network()` sessions never see these services. Inside a Docker container this
works too; processes in different containers need a shared network namespace (for example
`--network host`), as they do for any localhost connection.

This costs nothing that matters, because zero-copy is only important for large, frequent data:

- Camera frames and other large streams go over publish/subscribe, which stays on iceoryx2 with
  zero-copy loans.
- Service requests and responses are usually small and rare: a command, a configuration value, a
  trigger. A round trip takes about 80 µs instead of 27 µs (see [Performance](#performance)). The
  work that a service starts, such as a robot motion or an image capture, takes milliseconds or
  more, so the extra 50 µs is not noticeable.
- Large service payloads still work: Zenoh copies them, the same as on `Network()`.

In return, services have one implementation, so they behave the same on both transports.

## Processes, contexts and lifetime

The module-level functions use one default `Context` per process. A context owns the iceoryx2
node and the Zenoh sessions. Create one explicitly to control the lifetime:

```python
with ipc.Context() as ctx:
    pub = ctx.publisher("camera/front", CameraFrame, ipc.Local())
# every endpoint of ctx is closed here
```

- Create endpoints in the process that uses them. With `multiprocessing`, use the **spawn**
  start method, and create every shared object (`Event`, `Queue`, ...) from the spawn context:
  `multiprocessing.get_context("spawn").Event()`. A context refuses to work in a forked child.
- airo-ipc starts no threads of its own: call `receive()` and `server.spin_once()` in your own
  loop. Use each endpoint from one thread. `close()` and `Context.close()` are safe from any
  thread; a blocked `receive()` then raises `ClosedError`, and `spin_once()` returns False.
- A process that crashes, or is killed, leaves no stale shared memory behind: the next process
  cleans it up.

## Examples

| Example | Shows |
|---|---|
| `examples/pubsub.py [--network]` | pub/sub between two processes with loans |
| `examples/service.py [--network]` | a service, including a handler error |
| `examples/two_hosts.py` | streaming between two hosts with `--listen` / `--connect` |

## Performance

One Linux x86-64 machine, two processes, median one-way latency (`benchmarks/`):

| Workload | airo-ipc 0.3 | 1.0 `Local()` | 1.0 `Network()` (loopback) |
|---|---|---|---|
| Small message | 42 µs (spin-polling) | 40 µs | 97 µs |
| 900 KB image, copy on receive | — | 283 µs | 747 µs |
| 900 KB image, zero-copy read | 186 µs (spin-polling, can tear) | 157 µs (blocking) / 22 µs (spin-polling) | — |
| Service round trip | — | 80 µs (Zenoh on localhost) | 81 µs |

The 0.3 subscriber polls in a busy loop and uses a full CPU core. The 1.0 subscriber sleeps until
an event arrives; most of its latency is the operating system waking it up. Polling with
`receive(timeout=0)` in a loop is faster on `Local()`, but do not poll on `Network()`: it competes
with Zenoh's receive thread for the GIL.

## Limitations

- Messages have a fixed size. Variable-size messages (point clouds with a varying number of
  points, compressed images) are planned.
- Network messages are raw little-endian memory; big-endian hosts are not supported.
- Zenoh's own shared-memory transport is disabled by default.

## Migrating from 0.3

1.0 replaces the CycloneDDS + `multiprocessing.shared_memory` stack and its API. See
[MIGRATION.md](MIGRATION.md).

## Development

```bash
uv sync --all-groups
uv run pytest                       # every pub/sub and service test runs on Local() and Network()
uv run mypy airo_ipc                # strict
uv run black --check airo_ipc tests benchmarks examples
```
