"""Opinionated defaults for the AIRO lab's common IPC use cases.

These are picked for:
- Camera-frame streaming (one writer, many readers, want newest sample).
- Low-rate state (pose, setpoints).
- Control req/rep (a few callers, a few servers).

If your use case is different, override at the call site:

    pub = create_publisher("camera/frame", FrameBuffer, max_subscribers=16)
"""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class Iox2PubSubDefaults:
    """iceoryx2 publish/subscribe service sizing.

    - max_publishers=2: single-writer is the norm; 2 allows a brief handover (warm restart).
    - max_subscribers=8: covers "one camera, many consumers" without being wasteful.
    - history_size=1: newest-sample semantics; we want the latest frame, not a backlog.
    - subscriber_max_buffer_size=4: same family as issue #9 fix. Small enough to surface a
      stuck consumer; large enough to ride out a hiccup.
    - subscriber_max_borrowed_samples=2: keeps the "currently processing" + "next" pair.
    - enable_safe_overflow=True: drop oldest on overflow rather than block the publisher.
      Real-time loops can't tolerate publisher backpressure.
    """

    max_publishers: int = 2
    max_subscribers: int = 8
    history_size: int = 1
    subscriber_max_buffer_size: int = 4
    subscriber_max_borrowed_samples: int = 2
    enable_safe_overflow: bool = True


@dataclass(frozen=True)
class Iox2ReqRepDefaults:
    """iceoryx2 request/response service sizing.

    - max_clients=4: a few callers (e.g. an MPC, a teleop, a logger).
    - max_servers=1: the common case is one server per service. Warm-standby, hot-reload,
      and sharded backends are the only legitimate reasons to want more — override
      `max_servers=` at the call site for those.
    - max_active_requests_per_client=8: covers in-flight requests for slower handlers.
    """

    max_clients: int = 4
    max_servers: int = 1
    max_active_requests_per_client: int = 8


@dataclass(frozen=True)
class Iox2BlackboardDefaults:
    """iceoryx2 blackboard (state) service sizing.

    - max_readers=8: like pub/sub fanout.
    - max_nodes=8: same.
    """

    max_readers: int = 8
    max_nodes: int = 8


# Module-level singletons. Edit these in one place to tune lab-wide.
PUB_SUB = Iox2PubSubDefaults()
REQ_REP = Iox2ReqRepDefaults()
BLACKBOARD = Iox2BlackboardDefaults()


def merge_kwargs(defaults: Any, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Merge per-call overrides onto a defaults dataclass, returning the full kwarg set."""
    base = {f.name: getattr(defaults, f.name) for f in defaults.__dataclass_fields__.values()}
    unknown = set(overrides) - set(base)
    if unknown:
        raise TypeError(
            f"Unknown override(s) for {type(defaults).__name__}: {sorted(unknown)}. "
            f"Known fields: {sorted(base)}"
        )
    base.update(overrides)
    return base
