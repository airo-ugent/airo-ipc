# Worker framework

`Worker` is an optional convenience for long-running robotics processes that:

1. run at a fixed rate (for example 30 Hz for a camera, 200 Hz for a control loop),
2. publish, subscribe and serve services over `Local()` or `Network(...)`,
3. have a clear setup → loop → teardown lifecycle.

A `Worker` is a `multiprocessing` spawn process: each Worker runs in its own OS process. Its
endpoints are created in that process, because iceoryx2 nodes and Zenoh sessions do not survive
`fork`.

## Usage

```python
import airo_ipc as ipc
from airo_ipc.framework import Worker


class Value(ipc.Message):
    x: float


class Doubler(Worker):
    def _setup(self) -> None:
        """Runs once in the child process. Create endpoints here."""
        self._register_publisher("doubled", Value, ipc.Local())
        self._subscribe("values", Value, ipc.Local(), callback=self._on_value)
        self._serve("double", Value, Value, ipc.Local(), lambda v: Value(x=2 * v.x))

    def _on_value(self, msg: Value) -> None:
        self._publish("doubled", Value(x=2 * msg.x))

    def _step(self) -> None:
        """Runs at update_frequency Hz, after the callbacks and service requests."""

    def _teardown(self) -> None:
        """Runs once when the worker stops, also after an exception."""


if __name__ == "__main__":
    worker = Doubler(update_frequency=100.0, verbose=True)
    worker.start()
    ...
    worker.stop()
    worker.join()
```

## Loop

Each iteration:

1. For every subscription, deliver all received messages to the callback (with the default
   `Latest()` QoS, that is at most the newest one).
2. Handle all pending service requests.
3. Call `_step()`.
4. Sleep until the next period. If the iteration overran, the loop continues at once (it does not
   try to catch up); with `verbose=True` it logs the overrun.

Everything runs on one thread, so callbacks, handlers and `_step()` need no locks.

## Endpoint helpers

| Helper | Returns | Notes |
|---|---|---|
| `_register_publisher(topic, MsgType, transport, latched=False)` | `Publisher` | use `.loan()` on the result for zero-copy writes |
| `_publish(topic, msg)` | — | publish on a registered topic |
| `_publisher(topic)` | `Publisher` | the publisher of a registered topic |
| `_subscribe(topic, MsgType, transport, callback=..., qos=Latest())` | `Subscriber` | a callback may subscribe to more topics |
| `_serve(topic, Req, Rep, transport, handler)` | `Server` | handled on the loop thread |
| `_client(topic, Req, Rep, transport)` | `Client` | `call()` blocks the loop; prefer `call_async()` |

## Rules

- Share only spawn-context `multiprocessing` objects with a Worker:
  `multiprocessing.get_context("spawn").Event()`. On Python 3.11–3.13 the Linux default context
  is fork, and `worker.start()` raises `RuntimeError` for a fork-context `Event`, `Lock`, `Queue`
  or `Value`. Python 3.14 uses forkserver by default, where it happens to work, but follow the
  rule on every version so that the code also runs on older Pythons.
- The Worker class must be importable by the child process (define it at module level, and start
  it under `if __name__ == "__main__":`).
- A Worker is not required: endpoints work in any process, see `examples/framework/airo_mono.py`.
