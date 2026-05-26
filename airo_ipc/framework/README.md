# Worker framework

The `Worker` class is a convenience for long-running robotics processes that:

1. Tick at a fixed rate (e.g. 30 Hz for a camera, 200 Hz for a control loop).
2. Publish and subscribe to typed messages over iceoryx2 or Zenoh.
3. Have a clear setup → step-loop → teardown lifecycle.

It's a `multiprocessing.SpawnProcess` subclass: each `Worker` runs in its own OS process. Bundling
process lifecycle with IPC endpoints is deliberate — iceoryx2 Nodes and Zenoh Sessions must be
created in the process that uses them. `Worker` enforces this by construction rather than by
documentation.

## Usage

Subclass `Worker` and implement three methods:

```python
from airo_ipc import Buffer, Transport
from airo_ipc.framework import Worker


class MyWorker(Worker):
    def _setup(self):
        """Runs once in the child process before the step loop starts.
        Register publishers and subscribers here."""
        self._register_publisher("my/topic", MyBuffer, Transport.LOCAL)
        self._subscribe("other/topic", OtherBuffer, Transport.LOCAL,
                        callback=self._on_other)

    def _step(self):
        """Runs repeatedly at `update_frequency` Hz."""
        msg = MyBuffer(value=42)
        self._publish("my/topic", msg)

    def _teardown(self):
        """Runs once when the worker is stopping."""
        pass

    def _on_other(self, msg: OtherBuffer):
        """Subscription callbacks run before each _step()."""
        pass


if __name__ == "__main__":
    worker = MyWorker(update_frequency=30, verbose=True)
    worker.start()
    worker.join()
```

## What Worker is and isn't responsible for

**Is**: process lifecycle (start/stop), fixed-rate step loop, owns publishers/subscribers, dispatches
incoming messages to callbacks, warns on tick overruns.

**Is not**: discovery (transports do that), graph orchestration (Workers don't know about each other),
crash supervision (parent process's job), threading inside `_step()` (single-threaded by design).

If you need event-driven (not periodic) behavior, use the factory functions
(`create_publisher`, `create_subscriber`, etc.) directly instead of `Worker`.

## Examples

See `examples/framework/`.
