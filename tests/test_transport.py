"""Transport arguments, topic validation and context rules."""

from __future__ import annotations

import pytest

import airo_ipc as ipc
from airo_ipc.transport import ENV_CONNECT, ENV_LISTEN, ENV_NAMESPACE, validate_topic


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("192.168.1.20", "tcp/192.168.1.20:7447"),
        ("192.168.1.20:8000", "tcp/192.168.1.20:8000"),
        ("robot.local", "tcp/robot.local:7447"),
        ("[::1]", "tcp/[::1]:7447"),
        ("[::1]:9000", "tcp/[::1]:9000"),
        ("udp/10.0.0.2:7447", "udp/10.0.0.2:7447"),
        ("tcp/10.0.0.2:7447", "tcp/10.0.0.2:7447"),
    ],
)
def test_endpoint_normalization(given: str, expected: str) -> None:
    assert ipc.Network(connect=given).connect == (expected,)
    assert ipc.Network(listen=[given]).listen == (expected,)


def test_ambiguous_ipv6_endpoint() -> None:
    with pytest.raises(ValueError, match="IPv6"):
        ipc.Network(connect="::1")


def test_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CONNECT, "10.0.0.2, 10.0.0.3:8000")
    monkeypatch.setenv(ENV_LISTEN, "0.0.0.0")
    monkeypatch.setenv(ENV_NAMESPACE, "cell_2")
    resolved = ipc.Network().resolve()
    assert resolved.connect == ("tcp/10.0.0.2:7447", "tcp/10.0.0.3:8000")
    assert resolved.listen == ("tcp/0.0.0.0:7447",)
    assert resolved.namespace == "cell_2"
    explicit = ipc.Network(connect="10.9.9.9", namespace="lab").resolve()
    assert explicit.connect == ("tcp/10.9.9.9:7447",) and explicit.namespace == "lab"


def test_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NAMESPACE, raising=False)
    assert ipc.Network().resolve().namespace == "default"
    assert ipc.Network().resolve().key_prefix() == "airo_ipc/default"


def test_network_is_hashable_and_comparable() -> None:
    assert ipc.Network(connect="10.0.0.2") == ipc.Network(connect=["tcp/10.0.0.2:7447"])
    assert len({ipc.Network(), ipc.Network(), ipc.Local(), ipc.Local()}) == 2


def test_invalid_network_arguments() -> None:
    with pytest.raises(ValueError, match="mode"):
        ipc.Network(mode="router")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="namespace"):
        ipc.Network(namespace="a b")


@pytest.mark.parametrize(
    "topic", ["camera", "camera/front/rgb", "ur5e.joint_states", "a-b/c_d"]
)
def test_valid_topics(topic: str) -> None:
    assert validate_topic(topic) == topic


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "/camera",
        "camera/",
        "a//b",
        "a/*",
        "a/$b",
        "a?",
        "a#",
        "a b",
        "a/@evt",
        "..",
    ],
)
def test_invalid_topics(topic: str) -> None:
    with pytest.raises(ValueError, match="invalid topic"):
        validate_topic(topic)


class Point(ipc.Message):
    x: float


def test_context_argument_checks() -> None:
    with ipc.Context() as ctx:
        with pytest.raises(TypeError, match=r"pass an instance: Local\(\)"):
            ctx.publisher("t", Point, ipc.Local)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="Message subclass"):
            ctx.publisher("t", int, ipc.Local())  # type: ignore[type-var]
        with pytest.raises(ValueError, match="invalid topic"):
            ctx.publisher("/t", Point, ipc.Local())
        with pytest.raises(TypeError, match="qos"):
            ctx.subscriber("t", Point, ipc.Local(), qos=3)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="between 1 and"):
            ipc.Queue(0)


def test_abstract_message_rejected() -> None:
    from numpy.typing import NDArray
    import numpy as np

    class Template(ipc.Message):
        img: NDArray[np.uint8] = ipc.Array((None, 2), np.uint8)

    with ipc.Context() as ctx, pytest.raises(TypeError, match="with_shapes"):
        ctx.publisher("t", Template, ipc.Local())


def test_context_refuses_other_process() -> None:
    ctx = ipc.Context()
    ctx._pid -= 1  # pretend the context was inherited through fork()
    with pytest.raises(RuntimeError, match="created in process"):
        ctx.publisher("t", Point, ipc.Local())


def test_default_context_is_reused_and_recreated() -> None:
    first = ipc.default_context()
    assert ipc.default_context() is first
    first.close()
    assert ipc.default_context() is not first


def test_iox2_sizing_validation() -> None:
    with pytest.raises(ValueError, match="positive int"):
        ipc.Iox2Sizing(max_subscribers=0)
