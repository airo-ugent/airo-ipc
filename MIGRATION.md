# Migrating from v0.2.x to v1.0.0

v1.0.0 replaces the CycloneDDS + Python `multiprocessing.shared_memory` stack with iceoryx2
(local zero-copy) + Zenoh (network). The user-facing API is small but breaking. This guide
shows the before/after for the patterns you're likely using.

## Dependencies

```diff
- cyclonedds-nightly==2025.6.6
+ iceoryx2>=0.8.1
+ eclipse-zenoh>=1.7.2
```

## Message definitions

`BaseIdl` and `IdlStruct` are gone. Use `Buffer` (a `ctypes.Structure` subclass) for everything.

**Before** — separate types for DDS structs and shared-memory buffers:

```python
from cyclonedds.idl import IdlStruct
from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl


@dataclass
class ResolutionIdl(IdlStruct):
    width: int
    height: int


@dataclass
class WebcamFrame(BaseIdl):
    rgb: np.ndarray = np.empty((480, 640, 3), dtype=np.uint8)
```

**After** — one `Buffer` base for both transports, dataclass-shaped annotations:

```python
import ctypes
import numpy as np
from airo_ipc import Buffer


class Resolution(Buffer):
    _version_ = 1
    width: ctypes.c_int32
    height: ctypes.c_int32


class WebcamFrame(Buffer):
    _version_ = 1
    rgb: np.ndarray = np.zeros((480, 640, 3), dtype=np.uint8)
```

Annotation rules:

- `float` → `c_double`, `int` → `c_int64`, `bool` → `c_bool`. Use a `ctypes.c_*` type
  directly if you need a narrower width.
- `np.ndarray` fields require a class-level default of `np.zeros(shape, dtype=...)` —
  the metaclass reads the shape and dtype from it. The default is consumed during class
  construction and replaced by a property that returns a numpy view over shared memory.
- Scalar fields may have a default (`x: float = 1.5`) which is applied when an instance
  is constructed without that kwarg.
- For runtime-determined shapes (e.g. a queried camera resolution), use the factory
  form `buffer_from_arrays("Frame", version=1, rgb=np.zeros(shape, dtype=...))`.

The explicit form is still supported (e.g. for porting v0.2.x code in bulk):

```python
class Resolution(Buffer):
    _version_ = 1
    _fields_ = [("width", ctypes.c_int32), ("height", ctypes.c_int32)]
```

You can't mix `_fields_` and annotations in the same class — pick one.

Bump `_version_` whenever you change the layout so old subscribers raise
`IncompatibleVersionError` instead of silently reading corrupt data.

## Picking a transport

`IpcKind.DDS` and `IpcKind.SHARED_MEMORY` are gone. Use `Transport.NETWORK` and `Transport.LOCAL`:

```diff
- from airo_ipc.framework.framework import IpcKind
- IpcKind.DDS              # cross-machine
- IpcKind.SHARED_MEMORY    # same-machine, zero-copy
+ from airo_ipc import Transport
+ Transport.NETWORK        # cross-machine (Zenoh)
+ Transport.LOCAL          # same-machine, zero-copy (iceoryx2)
```

## Publishers and subscribers

Direct API (no framework):

**Before**:

```python
from cyclonedds.domain import DomainParticipant
from airo_ipc.cyclone_shm.patterns.ddswriter import DDSWriter
from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter

dp = DomainParticipant()
writer = SMWriter(dp, "frame", WebcamFrame())
writer(WebcamFrame(rgb=img))
```

**After**:

```python
from airo_ipc import Transport, create_publisher

pub = create_publisher("frame", WebcamFrame, Transport.LOCAL)
msg = WebcamFrame()
msg.rgb[:] = img
pub.publish(msg)
```

## Worker (formerly Node)

The `Node` class is renamed to `Worker` to avoid the name clash with `iceoryx2.Node`. The
`_subscribe` callback argument is now keyword-only.

**Before**:

```python
from airo_ipc.framework.node import Node
from airo_ipc.framework.framework import IpcKind


class MyNode(Node):
    def _setup(self):
        self._register_publisher("frame", WebcamFrame(), IpcKind.SHARED_MEMORY)
        self._subscribe("resolution", ResolutionIdl, IpcKind.DDS, self._on_resolution)

    def _on_resolution(self, msg):
        ...
```

**After**:

```python
from airo_ipc import Transport
from airo_ipc.framework import Worker


class MyWorker(Worker):
    def _setup(self):
        self._register_publisher("frame", WebcamFrame, Transport.LOCAL)
        self._subscribe("resolution", Resolution, Transport.NETWORK,
                        callback=self._on_resolution)

    def _on_resolution(self, msg):
        ...
```

Note: `_register_publisher` now takes a Buffer **class** (not an instance, as `SMWriter` did).
`_subscribe` takes `callback=` as a keyword-only argument.

## New patterns

v1.0.0 also exposes request/response and state writer/reader patterns that didn't exist before:

```python
from airo_ipc import create_requester, create_responder, create_state_writer, create_state_reader

# RPC-style
responder = create_responder("control/move_to", MoveRequest, MoveResponse, Transport.LOCAL)
responder.listen(handler)

requester = create_requester("control/move_to", MoveRequest, MoveResponse, Transport.LOCAL)
reply = requester.request(MoveRequest(x=1.0, y=2.0))

# Latest-known-value
writer = create_state_writer("robot/pose", PoseBuffer, Transport.LOCAL)
writer.set(PoseBuffer(...))

reader = create_state_reader("robot/pose", PoseBuffer, Transport.LOCAL)
state = reader.get()
```

## Sample-lifetime semantics

`SMReader` in v0.2.x intentionally returned references into shared memory (no copy), with the
caveat that you had to `.copy()` if you wanted to retain the data. In v1.0.0 the default
`receive()` **copies** — you own what you get back and can use it freely. If you need the
zero-copy fast path for high-throughput streaming, see the backend-specific notes.

## Versioning across the wire

`_ipc_version` is automatically prepended to every Buffer and checked on receive. If a publisher
and subscriber disagree on `_version_`, you get `IncompatibleVersionError` instead of silent
data corruption. This applies to all six patterns on both transports.

## Things removed

- `airo_ipc.cyclone_shm` package
- `airo_ipc.framework.framework.IpcKind`
- `airo_ipc.framework.node.Node` (renamed to `Worker`)
- `BaseIdl` / `BaseIDL`
- `SMReader.read_into` (use `receive()` directly; allocations are amortized differently)
- `WaitingForFirstMessageException` (subscribers return `None` instead)
