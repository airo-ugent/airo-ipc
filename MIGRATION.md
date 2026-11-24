# Migrating from airo-ipc 0.3 to 1.0

1.0 replaces CycloneDDS + Python `multiprocessing.shared_memory` with iceoryx2 (`Local()`, on one
host) and Zenoh (`Network(...)`, between hosts). The API changes completely, but it is smaller.
This page maps every 0.3 concept to its 1.0 replacement.

Why upgrade: 0.3 readers get views into memory that the writer reuses after three messages, so a
slow reader can see a half-written frame; segment creation can race; a reader started before its
writer waits forever; and a schema change between processes is silent corruption. 1.0 fixes all
four by design.

## Concept map

| 0.3 | 1.0 |
|---|---|
| `cyclonedds-nightly` dependency | `iceoryx2`, `eclipse-zenoh` (wheels, no daemon) |
| `BaseIdl` (numpy fields only, shared memory) | `ipc.Message` |
| `IdlStruct` (DDS messages) | `ipc.Message` (the same class works on both transports) |
| `DomainParticipant` | not needed (a `Context` is created for you) |
| `SMWriter(dp, topic, template)` | `ipc.publisher(topic, MsgType, ipc.Local())` |
| `SMReader(dp, topic, template)` | `ipc.subscriber(topic, MsgType, ipc.Local())` |
| `DDSWriter(dp, topic, IdlType)` | `ipc.publisher(topic, MsgType, ipc.Network(...))` |
| `DDSReader(dp, topic, IdlType)` | `ipc.subscriber(topic, MsgType, ipc.Network(...))` |
| `writer(msg)` | `pub.publish(msg)`, or `with pub.loan() as msg: ...` (zero-copy) |
| `reader()` (returns a view that can change under you) | `sub.receive()` (an owned copy) or `with sub.receive_loan() as msg:` (a stable view) |
| `reader.read_into(msg)` | `with sub.receive_loan() as view: np.copyto(dst, view.field)` |
| `DDSReader.take()` | `sub.receive()` with `ipc.subscriber(..., depth=n)` |
| `DDSReader.__call__()` (latest without consuming) | `sub.receive()` with the default `depth=1` |
| `WaitingForFirstMessageException` | `receive()` returns `None` |
| `nr_of_buffers` | gone: iceoryx2 tracks who holds which buffer |
| (none) | services: `ipc.server(...)` / `ipc.client(...).call(...)` |
| `airo_ipc.framework.Node` | removed: write the loop yourself, see [Node](#node) |
| `IpcKind.SHARED_MEMORY` / `IpcKind.DDS` | `ipc.Local()` / `ipc.Network(...)` |

## Message definitions

**0.3**: two kinds of types.

```python
from dataclasses import dataclass, field
import numpy as np
from cyclonedds.idl import IdlStruct
from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl


@dataclass
class ResolutionIdl(IdlStruct):
    width: int
    height: int


@dataclass
class WebcamFrame(BaseIdl):
    rgb: np.ndarray = field(default_factory=lambda: np.zeros((480, 640, 3), dtype=np.uint8))
```

**1.0**: one base class; scalars and arrays can be mixed.

```python
import numpy as np
from numpy.typing import NDArray
import airo_ipc as ipc


class Resolution(ipc.Message):
    width: ipc.i32
    height: ipc.i32


class WebcamFrame(ipc.Message):
    stamp: float
    rgb: NDArray[np.uint8] = ipc.Array((480, 640, 3), np.uint8)
```

Shapes known only at runtime: declare `None` dimensions and call `with_shapes`:

```python
class WebcamFrame(ipc.Message):
    rgb: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)

Frame = WebcamFrame.with_shapes(rgb=(height, width, 3))
```

## Shared-memory streaming

**0.3**

```python
from cyclonedds.domain import DomainParticipant
from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter
from airo_ipc.cyclone_shm.patterns.sm_reader import SMReader

dp = DomainParticipant()
writer = SMWriter(dp, "frame", WebcamFrame())
msg = WebcamFrame()
msg.rgb[:] = image
writer(msg)

reader = SMReader(dp, "frame", WebcamFrame())   # blocks forever until a writer exists
frame = reader()                                 # a view: copy it at once or risk tearing
rgb = frame.rgb.copy()
```

**1.0**

```python
import airo_ipc as ipc

pub = ipc.publisher("frame", WebcamFrame, ipc.Local())
with pub.loan() as msg:          # writes straight into shared memory
    msg.rgb[:] = image

sub = ipc.subscriber("frame", WebcamFrame, ipc.Local())   # does not need a publisher yet
frame = sub.receive(timeout=1.0)  # an owned copy, or None
```

## DDS messages

**0.3**

```python
from airo_ipc.cyclone_shm.patterns.ddswriter import DDSWriter
from airo_ipc.cyclone_shm.patterns.ddsreader import DDSReader

writer = DDSWriter(dp, "resolution", ResolutionIdl)
writer(ResolutionIdl(width=640, height=480))
reader = DDSReader(dp, "resolution", ResolutionIdl)
resolution = reader()
```

**1.0**

```python
class GetResolution(ipc.Message):     # an empty request
    pass

# The camera process serves its resolution, in its own loop.
server = ipc.server("resolution", GetResolution, Resolution, ipc.Network(),
                    lambda _: Resolution(width=640, height=480))
while running:
    server.spin_once(timeout=0.1)

# Another process asks for it, also if it starts later than the camera process.
client = ipc.client("resolution", GetResolution, Resolution, ipc.Network())
client.wait_for_server(timeout=5.0)
resolution = client.call(GetResolution())
```

A subscriber receives only messages published after it connected, so use a service (not a topic)
for state that a process needs when it starts.

## Node

1.0 has no `Node` class. Use a `multiprocessing` process with a loop; `_setup`, `_step` and
`_teardown` become plain code before, in and after the loop:

```python
import multiprocessing
import time

import airo_ipc as ipc


def camera_process(stop) -> None:
    camera = open_camera()                                                   # _setup
    frames = ipc.publisher("camera/front", CameraFrame, ipc.Local())
    commands = ipc.subscriber("camera/commands", Command, ipc.Local(), depth=8)
    period = 1 / 30
    try:
        while not stop.is_set():
            start = time.monotonic()
            while (command := commands.receive()) is not None:              # _subscribe callback
                handle(command)
            with frames.loan() as frame:                                     # _step
                camera.read_into(frame.rgb)
            time.sleep(max(0.0, period - (time.monotonic() - start)))
    finally:
        camera.close()                                                       # _teardown


if __name__ == "__main__":
    spawn = multiprocessing.get_context("spawn")
    stop = spawn.Event()
    process = spawn.Process(target=camera_process, args=(stop,))
    process.start()
    ...
    stop.set()
    process.join()
```

Create endpoints in the child process (iceoryx2 and Zenoh resources do not survive `fork`), and
create every `multiprocessing` object that you share with it from
`multiprocessing.get_context("spawn")`.

## Behaviour changes to check

- **Python 3.11+** (0.3: 3.9+).
- **Threads.** airo-ipc starts no threads: call `receive()` and `server.spin_once()` in your own
  loop, and use each endpoint from one thread.
- **Copies.** `receive()` returns a copy. Use `receive_loan()` where the copy matters.
- **Schema.** Changing a message class (field, type, shape, order or class name) makes old and
  new processes incompatible; they raise `SchemaMismatchError` instead of exchanging garbage.
  Restart all processes that use the topic together.
- **Topic names** use letters, digits, `_`, `-`, `.` and `/` separators.
- **Logging** uses the standard `logging` module (logger name `"airo_ipc"`); loguru is no longer
  a dependency.
