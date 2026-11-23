"""Publish/subscribe behaviour, identical on Local() and Network()."""

from __future__ import annotations

import gc
import threading
import time

import numpy as np
import pytest
from numpy.typing import NDArray

import airo_ipc as ipc
from conftest import Pair, wait_until


class Frame(ipc.Message):
    stamp: float
    index: ipc.i32
    rgb: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)


Small = Frame.with_shapes(rgb=(4, 6, 3))
Other = Frame.with_shapes(rgb=(6, 4, 3))


def connect(pair: Pair, topic: str, msg_type: type[ipc.Message] = Small, **sub_kwargs: object):  # type: ignore[no-untyped-def]
    """A publisher on A and a subscriber on B that see each other."""
    sub = pair.b.subscriber(topic, msg_type, pair.tb, **sub_kwargs)  # type: ignore[arg-type]
    pub = pair.a.publisher(topic, msg_type, pair.ta)
    assert wait_until(lambda: pub.has_subscribers and sub.has_publishers)
    return pub, sub


def test_roundtrip(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    before = time.time_ns()
    pub.publish(Small(stamp=1.5, index=7, rgb=9))
    msg = sub.receive(timeout=5)
    assert msg is not None
    assert (msg.stamp, msg.index, int(msg.rgb.sum())) == (1.5, 7, 9 * 4 * 6 * 3)
    assert msg.meta.seq == 1 and before <= msg.meta.stamp_ns <= time.time_ns()
    msg.rgb[:] = 0  # receive() returns an owned, writable copy
    pub.publish(Small(stamp=2.0, index=8))
    second = sub.receive(timeout=5)
    assert second is not None and second.meta.seq == 2


def test_receive_without_message(pair: Pair, topic: str) -> None:
    _, sub = connect(pair, topic)
    assert sub.receive() is None
    start = time.monotonic()
    assert sub.receive(timeout=0.2) is None
    assert 0.18 <= time.monotonic() - start < 1.0


def test_blocking_receive_wakes_up(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    timer = threading.Timer(0.1, lambda: pub.publish(Small(stamp=3.0, index=1)))
    start = time.monotonic()
    timer.start()
    msg = sub.receive(timeout=None)
    elapsed = time.monotonic() - start
    assert msg is not None and msg.stamp == 3.0
    assert elapsed < 0.1 + 0.08  # event-based wake-up, not polling


def test_latest_keeps_only_newest(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    for i in range(5):
        pub.publish(Small(stamp=0.0, index=i))
    time.sleep(0.3)
    msg = sub.receive(timeout=1)
    assert msg is not None and msg.index == 4
    assert sub.receive() is None


def test_queue_keeps_order_and_drops_oldest(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic, qos=ipc.Queue(3))
    for i in range(5):
        pub.publish(Small(stamp=0.0, index=i))
    time.sleep(0.3)
    received = []
    while (msg := sub.receive(timeout=0.2)) is not None:
        received.append(msg.index)
    assert received == [2, 3, 4]


def test_loan_publish(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    with pub.loan() as frame:
        frame.stamp = 4.0
        frame.index = 3
        frame.rgb[:] = 5
        view = frame.rgb
    assert not view.flags.writeable  # read-only after the block
    with pytest.raises(ipc.LoanReleasedError):
        frame.index = 4
    msg = sub.receive(timeout=5)
    assert msg is not None and (msg.stamp, msg.index, int(msg.rgb[0, 0, 0])) == (
        4.0,
        3,
        5,
    )


def test_loan_not_sent_on_exception(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    with pytest.raises(RuntimeError):
        with pub.loan() as frame:
            frame.index = 1
            raise RuntimeError("abort")
    with pub.loan() as frame:  # the memory went back to the pool
        frame.stamp = 0.0
        frame.index = 2
        frame.rgb[:] = 0
    msg = sub.receive(timeout=5)
    assert msg is not None and msg.index == 2 and msg.meta.seq == 1


def test_receive_loan(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    pub.publish(Small(stamp=5.0, index=1, rgb=6))
    with sub.receive_loan(timeout=5) as frame:
        assert frame is not None
        assert frame.stamp == 5.0 and int(frame.rgb[1, 1, 1]) == 6
        assert not frame.rgb.flags.writeable
        with pytest.raises(ipc.ReadOnlyMessageError):
            frame.index = 2
        copy = frame.copy()
    with pytest.raises(ipc.LoanReleasedError):
        _ = frame.rgb
    copy.index = 3  # a copy is writable and stays valid
    with sub.receive_loan() as nothing:
        assert nothing is None


def test_receive_loan_view_outlives_block(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    pub.publish(Small(stamp=0.0, index=1, rgb=7))
    with sub.receive_loan(timeout=5) as frame:
        assert frame is not None
        kept = frame.rgb
    for i in range(10):  # the publisher must not reuse the borrowed memory
        pub.publish(Small(stamp=0.0, index=i, rgb=0))
    time.sleep(0.1)
    assert np.all(kept == 7)
    del kept
    gc.collect()


def test_too_many_loans_local(topic: str) -> None:
    with ipc.Context(iox2=ipc.Iox2Sizing(max_borrowed_per_subscriber=2)) as ctx:
        sub = ctx.subscriber(topic, Small, ipc.Local(), qos=ipc.Queue(8))
        pub = ctx.publisher(topic, Small, ipc.Local())
        kept = []
        for i in range(3):
            pub.publish(Small(stamp=0.0, index=i))
        for _ in range(2):
            with sub.receive_loan(timeout=1) as frame:
                assert frame is not None
                kept.append(frame.rgb)
        with pytest.raises(ipc.TooManyLoansError):
            sub.receive()
        kept.clear()
        gc.collect()
        assert sub.receive() is not None


def test_has_publishers_and_subscribers(pair: Pair, topic: str) -> None:
    pub = pair.a.publisher(topic, Small, pair.ta)
    assert not pub.has_subscribers
    sub = pair.b.subscriber(topic, Small, pair.tb)
    assert wait_until(lambda: pub.has_subscribers and sub.has_publishers)
    pub.close()
    assert wait_until(lambda: not sub.has_publishers)


def test_latched_reaches_late_subscriber(pair: Pair, topic: str) -> None:
    pub = pair.a.publisher(topic, Small, pair.ta, latched=True)
    pub.publish(Small(stamp=0.0, index=42))
    time.sleep(0.1)
    sub = pair.b.subscriber(topic, Small, pair.tb)
    msg = sub.receive(timeout=3)
    assert msg is not None and msg.index == 42


def test_not_latched_skips_late_subscriber(pair: Pair, topic: str) -> None:
    pub = pair.a.publisher(topic, Small, pair.ta)
    pub.publish(Small(stamp=0.0, index=42))
    time.sleep(0.1)
    sub = pair.b.subscriber(topic, Small, pair.tb)
    assert wait_until(lambda: sub.has_publishers)
    assert sub.receive(timeout=0.5) is None
    pub.publish(Small(stamp=0.0, index=43))
    msg = sub.receive(timeout=3)
    assert msg is not None and msg.index == 43


def test_schema_mismatch(pair: Pair, topic: str) -> None:
    pub = pair.a.publisher(topic, Small, pair.ta)
    if not pair.is_network:
        with pytest.raises(ipc.SchemaMismatchError):
            pair.b.subscriber(topic, Other, pair.tb)
        return
    sub = pair.b.subscriber(topic, Other, pair.tb)
    assert wait_until(lambda: pub.has_subscribers)
    pub.publish(Small(stamp=0.0, index=1))
    with pytest.raises(ipc.SchemaMismatchError):
        sub.receive(timeout=3)


def test_on_message(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic, qos=ipc.Queue(16))
    received: list[int] = []
    done = threading.Event()

    def callback(msg: ipc.Message) -> None:
        received.append(msg.index)  # type: ignore[attr-defined]
        if len(received) == 3:
            done.set()

    sub.on_message(callback)
    for i in range(3):
        pub.publish(Small(stamp=0.0, index=i))
        time.sleep(0.01)
    assert done.wait(5)
    assert received == [0, 1, 2]
    sub.close()


def test_large_frame(pair: Pair, topic: str) -> None:
    Big = Frame.with_shapes(rgb=(720, 1280, 3))
    pub, sub = connect(pair, topic, msg_type=Big)
    image = np.random.default_rng(0).integers(
        0, 255, size=(720, 1280, 3), dtype=np.uint8
    )
    with pub.loan() as frame:
        frame.stamp = 0.0
        frame.index = 1
        frame.rgb[:] = image
    msg = sub.receive(timeout=5)
    assert msg is not None and np.array_equal(msg.rgb, image)


def test_publish_wrong_type(pair: Pair, topic: str) -> None:
    pub = pair.a.publisher(topic, Small, pair.ta)
    with pytest.raises(TypeError, match="expects a Frame"):
        pub.publish(Other(stamp=0.0, index=0))  # type: ignore[arg-type]


def test_closed_endpoints(pair: Pair, topic: str) -> None:
    pub, sub = connect(pair, topic)
    pub.close()
    with pytest.raises(ipc.ClosedError):
        pub.publish(Small(stamp=0.0, index=0))
    pair.b.close()
    assert sub.closed
    with pytest.raises(ipc.ClosedError):
        sub.receive()
    with pytest.raises(ipc.ClosedError):
        pair.b.subscriber(topic, Small, pair.tb)


def test_close_unblocks_waiting_receive(pair: Pair, topic: str) -> None:
    _, sub = connect(pair, topic)
    errors: list[BaseException] = []

    def wait_forever() -> None:
        try:
            sub.receive(timeout=None)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=wait_forever)
    thread.start()
    time.sleep(0.1)
    sub.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ipc.ClosedError)
