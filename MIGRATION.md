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
| `DDSReader.take()` | `sub.receive()` with `qos=ipc.Queue(n)` |
| `DDSReader.__call__()` (latest without consuming) | `sub.receive()` with `qos=ipc.Latest()` |
| `WaitingForFirstMessageException` | `receive()` returns `None` |
| `nr_of_buffers` | gone: iceoryx2 tracks who holds which buffer |
| (none) | services: `ipc.server(...)` / `ipc.client(...).call(...)` |
| `airo_ipc.framework.Node` | `airo_ipc.framework.Worker` |
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
pub = ipc.publisher("resolution", Resolution, ipc.Network(), latched=True)
pub.publish(Resolution(width=640, height=480))
sub = ipc.subscriber("resolution", Resolution, ipc.Network())
resolution = sub.receive(timeout=5.0)   # latched: also arrives if the subscriber starts later
```

On one host you can use `ipc.Local()` for small messages too; it is faster than `Network()`.

## Node → Worker

| 0.3 `Node` | 1.0 `Worker` |
|---|---|
| `Node(update_frequency, verbose)` | `Worker(update_frequency, verbose)` |
| `_register_publisher(topic, idl, IpcKind.X)` | `_register_publisher(topic, MsgType, transport, latched=False)` (returns the publisher) |
| `_publish(topic, value)` | `_publish(topic, msg)`, or `self._publisher(topic).loan()` |
| `_subscribe(topic, idl, IpcKind.X, callback)` | `_subscribe(topic, MsgType, transport, callback=..., qos=...)` |
| (none) | `_serve(topic, Req, Rep, transport, handler)`, `_client(topic, Req, Rep, transport)` |
| `_setup` / `_step` / `_teardown` / `stop()` | unchanged |

Differences:

- The transport is a required argument; there is no default.
- Registering the same topic twice raises `ValueError` (0.3 logged a warning and ignored it).
- The callback receives an owned copy of each message; several messages per loop iteration are
  delivered when a `Queue` QoS is used.
- `_teardown()` also runs when `_setup()` or `_step()` raises.

The spawn rule is unchanged: create every `multiprocessing` object that you share with a Worker
from `multiprocessing.get_context("spawn")`.

## Behaviour changes to check

- **Python 3.11+** (0.3: 3.9+).
- **Copies.** `receive()` returns a copy. Use `receive_loan()` where the copy matters.
- **Schema.** Changing a message class (field, type, shape, order or class name) makes old and
  new processes incompatible; they raise `SchemaMismatchError` instead of exchanging garbage.
  Restart all processes that use the topic together.
- **Topic names** use letters, digits, `_`, `-`, `.` and `/` separators.
- **Logging** uses the standard `logging` module (logger name `"airo_ipc"`); loguru is no longer
  a dependency.
