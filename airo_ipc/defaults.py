"""Sizing defaults for the iceoryx2 (``Local()``) transport.

iceoryx2 fixes the size of a topic or service when the first process creates it; later processes
may only ask for the same size or less. Therefore every process must use the same sizing, and the
defaults are generous. Generous sizes are cheap: iceoryx2 reserves the worst-case shared memory,
but the operating system only backs the chunks that are written. Measured with iceoryx2 0.10.1 for
a 4.6 MB message (1280x720 RGB-D) and 8 subscribers: 235 MB reserved, 23 MB used after 30 sends.

Change the sizing only for all processes of a system at once, with ``Context(iox2=Iox2Sizing(...))``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Iox2Sizing"]


@dataclass(frozen=True)
class Iox2Sizing:
    """iceoryx2 sizes for topics and services.

    Attributes:
        max_publishers: Publishers per topic. More than one allows a warm restart.
        max_subscribers: Subscribers per topic.
        max_borrowed_per_subscriber: ``receive_loan()`` views that one subscriber may keep alive
            at the same time.
        max_clients: Clients per service.
        max_servers: Servers per service. 1: a second server for the same service fails loudly.
        max_active_requests_per_client: Requests that one client may have in flight.
    """

    max_publishers: int = 4
    max_subscribers: int = 16
    max_borrowed_per_subscriber: int = 4
    max_clients: int = 16
    max_servers: int = 1
    max_active_requests_per_client: int = 8

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"Iox2Sizing.{name} must be a positive int, got {value!r}"
                )
