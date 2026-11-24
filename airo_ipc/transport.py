"""Transport selection: ``Local()`` (this host) or ``Network(...)`` (between hosts, Zenoh).

Every factory takes the transport as a required argument, so the code always shows where the data
goes::

    pub = ipc.publisher("camera/front", CameraFrame, ipc.Local())
    pub = ipc.publisher("camera/front", CameraFrame, ipc.Network(connect="192.168.1.20"))

The topic string and the message class are the same for both transports.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Local",
    "Network",
    "ResolvedNetwork",
    "Transport",
    "validate_topic",
    "DEFAULT_PORT",
    "DEFAULT_NAMESPACE",
    "ENV_CONNECT",
    "ENV_LISTEN",
    "ENV_NAMESPACE",
]

#: Environment variable with comma-separated endpoints, used when `Network(connect=...)` is empty.
ENV_CONNECT = "AIRO_IPC_CONNECT"
#: Environment variable with comma-separated endpoints, used when `Network(listen=...)` is empty.
ENV_LISTEN = "AIRO_IPC_LISTEN"
#: Environment variable with the namespace, used when `Network(namespace=...)` is not given.
ENV_NAMESPACE = "AIRO_IPC_NAMESPACE"

#: Zenoh's default port; added to endpoints that have no port.
DEFAULT_PORT = 7447
#: Namespace used when neither `Network(namespace=...)` nor `ENV_NAMESPACE` is set.
DEFAULT_NAMESPACE = "default"

_SEGMENT = re.compile(r"[A-Za-z0-9_\-.]+")


def validate_topic(topic: str) -> str:
    """Check that ``topic`` is valid for both iceoryx2 and Zenoh, and return it.

    A topic is one or more segments separated by ``/``. A segment uses only letters, digits,
    ``_``, ``-`` and ``.``. Examples: ``"camera/front/rgb"``, ``"ur5e.joint_states"``.

    Raises:
        ValueError: the topic is empty or contains other characters.
    """
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"invalid topic {topic!r}: a topic must be a non-empty string")
    for segment in topic.split("/"):
        if not _SEGMENT.fullmatch(segment) or segment in (".", ".."):
            raise ValueError(
                f"invalid topic {topic!r}: use segments of letters, digits, '_', '-' and '.' "
                f"separated by single '/' (no leading or trailing '/')"
            )
    return topic


@dataclass(frozen=True)
class Local:
    """Between processes on this host.

    Publish/subscribe uses iceoryx2 zero-copy shared memory. Services (client/server) use Zenoh on
    localhost, which never leaves this host; see "Services on Local()" in the README for why.

    """

    def resolve(self) -> ResolvedNetwork:
        """The Zenoh settings of services on this host (see :data:`_LOCAL_SERVICES`)."""
        return _LOCAL_SERVICES


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("empty endpoint")
    if "/" in endpoint:
        return endpoint  # already a Zenoh locator such as "tcp/10.0.0.2:7447"
    # A bare host or host:port is a TCP endpoint.
    if endpoint.startswith("["):  # IPv6, "[::1]" or "[::1]:7447"
        return (
            f"tcp/{endpoint}" if "]:" in endpoint else f"tcp/{endpoint}:{DEFAULT_PORT}"
        )
    if endpoint.count(":") == 1:
        return f"tcp/{endpoint}"
    if ":" in endpoint:
        raise ValueError(
            f"ambiguous endpoint {endpoint!r}: write IPv6 addresses as 'tcp/[addr]:port'"
        )
    return f"tcp/{endpoint}:{DEFAULT_PORT}"


def _endpoints(value: str | Sequence[str]) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else list(value)
    return tuple(_normalize_endpoint(e) for e in items if e.strip())


def _env_endpoints(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return _endpoints([part for part in raw.split(",") if part.strip()])


def _validate_namespace(namespace: str) -> str:
    try:
        return validate_topic(namespace)
    except ValueError:
        raise ValueError(
            f"invalid namespace {namespace!r}: use letters, digits, '_', '-', '.' and '/'"
        ) from None


@dataclass(frozen=True)
class ResolvedNetwork:
    """A :class:`Network` with defaults and environment variables applied."""

    connect: tuple[str, ...]
    listen: tuple[str, ...]
    namespace: str
    multicast: bool | None
    mode: Literal["peer", "client"] | None
    config: str | None
    #: Interface (name or IP address) and ``group:port`` for multicast discovery; None: Zenoh's.
    multicast_interface: str | None = None
    multicast_address: str | None = None

    def session_key(self) -> tuple[object, ...]:
        """Endpoints that share this key share one Zenoh session."""
        return (
            self.connect,
            self.listen,
            self.multicast,
            self.mode,
            self.config,
            self.multicast_interface,
            self.multicast_address,
        )

    def key_prefix(self) -> str:
        return f"airo_ipc/{self.namespace}"


#: Zenoh settings for services on ``Local()``. Every process listens on a random port on
#: 127.0.0.1, and the processes find each other with multicast on the loopback interface (given
#: as an IP address, so the interface name does not matter). The discovery port differs from
#: Zenoh's default (7446), so ``Network()`` sessions never see these peers, and the "@local"
#: namespace cannot collide with a ``Network(namespace=...)``, which allows no "@".
_LOCAL_SERVICES = ResolvedNetwork(
    connect=(),
    listen=("tcp/127.0.0.1:0",),
    namespace="@local",
    multicast=True,
    mode="peer",
    config=None,
    multicast_interface="127.0.0.1",
    multicast_address="224.0.0.224:7448",
)


@dataclass(frozen=True, init=False)
class Network:
    """Zenoh transport: between hosts, and between processes on one host.

    Without arguments, Zenoh peers on the same LAN find each other with multicast discovery.
    Give an endpoint when multicast is blocked (common on university or corporate Wi-Fi), across
    subnets, or through a Zenoh router.

    Examples::

        Network()                                   # multicast discovery
        Network(connect="192.168.1.20")             # = "tcp/192.168.1.20:7447"
        Network(listen="0.0.0.0:7447")              # accept connections on port 7447
        Network(connect="10.0.0.2", namespace="cell_2")
        Network(config="zenoh.json5")               # a full Zenoh configuration file

    Args:
        connect: Endpoints to connect to. A bare ``host`` or ``host:port`` becomes
            ``tcp/host:port`` (port 7447 by default); full Zenoh locators such as
            ``"udp/10.0.0.2:7447"`` are kept. Empty: use ``$AIRO_IPC_CONNECT`` if set.
        listen: Endpoints to listen on, same format. Empty: use ``$AIRO_IPC_LISTEN`` if set.
        namespace: Prefix that isolates groups of nodes on one network (like a ROS domain).
            ``None``: use ``$AIRO_IPC_NAMESPACE``, or ``"default"``.
        multicast: Enable (True) or disable (False) Zenoh multicast discovery. ``None``: Zenoh's
            default (enabled in peer mode), or the value in ``config``.
        mode: Zenoh session mode, ``"peer"`` or ``"client"`` (only via a router). ``None``:
            ``"peer"``, or the value in ``config``.
        config: Path to a Zenoh JSON5 configuration file. The other arguments are applied on
            top of it.
    """

    connect: tuple[str, ...]
    listen: tuple[str, ...]
    namespace: str | None
    multicast: bool | None
    mode: Literal["peer", "client"] | None
    config: str | None

    def __init__(
        self,
        connect: str | Sequence[str] = (),
        listen: str | Sequence[str] = (),
        *,
        namespace: str | None = None,
        multicast: bool | None = None,
        mode: Literal["peer", "client"] | None = None,
        config: str | os.PathLike[str] | None = None,
    ) -> None:
        if mode not in (None, "peer", "client"):
            raise ValueError(f"invalid mode {mode!r}: use 'peer' or 'client'")
        object.__setattr__(self, "connect", _endpoints(connect))
        object.__setattr__(self, "listen", _endpoints(listen))
        object.__setattr__(
            self,
            "namespace",
            None if namespace is None else _validate_namespace(namespace),
        )
        object.__setattr__(
            self, "multicast", None if multicast is None else bool(multicast)
        )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self, "config", None if config is None else os.fspath(config)
        )

    def resolve(self) -> ResolvedNetwork:
        """Apply the environment variables and defaults."""
        namespace = self.namespace or os.environ.get(ENV_NAMESPACE) or DEFAULT_NAMESPACE
        return ResolvedNetwork(
            connect=self.connect or _env_endpoints(ENV_CONNECT),
            listen=self.listen or _env_endpoints(ENV_LISTEN),
            namespace=_validate_namespace(namespace),
            multicast=self.multicast,
            mode=self.mode,
            config=self.config,
        )


#: The transport argument of every factory: ``Local()`` or ``Network(...)``.
Transport = Local | Network
