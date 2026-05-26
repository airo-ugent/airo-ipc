# airo-ipc

IPC framework for the AIRO lab. Provides easy, fast inter-process communication for robotics
applications in Python, with a single API that hides the choice of transport.

`airo-ipc` is a thin opinionated layer over two underlying libraries:

- **[iceoryx2](https://iceoryx2.org/)** for local zero-copy shared memory IPC (sub-microsecond
  latency on the same machine).
- **[Zenoh](https://zenoh.io/)** for cross-machine pub/sub, request/response, and state sharing.

You write your messages once as a typed `Buffer`, choose `Transport.LOCAL` or `Transport.NETWORK`
per topic, and `airo-ipc` handles the rest — including cross-process version checks, sensible
defaults for AIRO-lab use cases (camera frames at 60 Hz, low-rate state, control req/rep), and a
`Worker` framework class that bundles a step loop with its IPC endpoints.

## Main features

- **Buffer**: define typed messages (including big numpy arrays) once, use them on both transports.
- **Six communication patterns**: publisher/subscriber, requester/responder, state writer/reader.
- **Worker**: a `multiprocessing.SpawnProcess` subclass with a fixed-rate step loop, publish/subscribe
  helpers, and lifecycle hooks.
- **Cross-process version safety**: `_ipc_version` field automatically checked on every receive.
- **Single `pip install`**: no system daemons, no C++ runtime — both backends are pure-Rust libraries
  with Python wheels.

## Installation

```bash
pip install airo-ipc
```

Or with `uv`:

```bash
uv add airo-ipc
```

Or from source:

```bash
git clone https://github.com/airo-ugent/airo-ipc
cd airo-ipc && uv sync
```

Python 3.10+ required.

## Quick start

```python
import ctypes
from airo_ipc import Buffer, Transport, create_publisher, create_subscriber


class Point(Buffer):
    _version_ = 1
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


# Process A
pub = create_publisher("waypoint", Point, Transport.LOCAL)
pub.publish(Point(x=1.0, y=2.0))

# Process B
sub = create_subscriber("waypoint", Point, Transport.LOCAL)
msg = sub.receive()
print(msg.x, msg.y)
```

For big numpy buffers, generate a Buffer subclass with `buffer_from_arrays`:

```python
import numpy as np
from airo_ipc import buffer_from_arrays

FrameBuffer = buffer_from_arrays(
    "FrameBuffer", version=1,
    bgr=np.zeros((480, 640, 3), dtype=np.uint8),
)

buf = FrameBuffer()
buf.bgr[:] = my_image  # numpy assignment into the shared-memory-backed struct
```

## Patterns

| Pattern              | Factory                | When to use                                          |
|----------------------|------------------------|------------------------------------------------------|
| publisher/subscriber | `create_publisher`     | Streaming data (camera frames, sensor readings)      |
|                      | `create_subscriber`    |                                                      |
| requester/responder  | `create_requester`     | RPC-style calls (control commands, queries)          |
|                      | `create_responder`     |                                                      |
| state writer/reader  | `create_state_writer`  | Latest-known value (robot pose, current setpoint)    |
|                      | `create_state_reader`  |                                                      |

All six factories accept the same `(topic, buffer_type, transport)` arguments.

## The Worker framework

For long-running processes that tick at a fixed rate, use the `Worker` class:

```python
from airo_ipc import Buffer, Transport
from airo_ipc.framework import Worker


class CameraNode(Worker):
    def _setup(self):
        self._register_publisher("camera/frame", FrameBuffer, Transport.LOCAL)

    def _step(self):
        msg = FrameBuffer()
        msg.bgr[:] = self._read_camera()
        self._publish("camera/frame", msg)

    def _teardown(self):
        pass


if __name__ == "__main__":
    CameraNode(update_frequency=30).start()
```

See [the framework README](airo_ipc/framework/README.md) for details and
`examples/framework/` for runnable examples.

## Examples

- `examples/framework/webcam.py` — publisher/subscriber webcam frames over iceoryx2 with
  resolution over Zenoh.
- `examples/framework/airo_mono.py` — same but exposing the airo-camera-toolkit `RGBCamera`
  interface to the subscriber.

## Migration from v0.2.x

v1.0.0 is a hard break from v0.2.x (CycloneDDS + manual shared memory). See
[MIGRATION.md](MIGRATION.md) for a one-page guide.
