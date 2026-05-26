"""Unit tests for Buffer base class and buffer_from_arrays helper."""

import ctypes

import numpy as np
import pytest

from airo_ipc.buffer import Buffer, IncompatibleVersionError, buffer_from_arrays


class SimpleBuffer(Buffer):
    _version_ = 1
    _fields_ = [
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
    ]


class ArrayBuffer(Buffer):
    _version_ = 2
    _fields_ = [
        ("data", ctypes.c_uint8 * 16),
    ]


def test_version_field_present():
    buf = SimpleBuffer(x=1.0, y=2.0)
    assert buf._ipc_version == 1


def test_serialization_roundtrip():
    buf = SimpleBuffer(x=3.14, y=2.72)
    data = buf.to_bytes()
    restored = SimpleBuffer.from_bytes(data)
    assert restored.x == pytest.approx(3.14)
    assert restored.y == pytest.approx(2.72)
    assert restored._ipc_version == 1


def test_from_bytes_wrong_size():
    with pytest.raises(ValueError, match="Expected"):
        SimpleBuffer.from_bytes(b"short")


def test_version_check_pass():
    buf = SimpleBuffer(x=1.0, y=2.0)
    SimpleBuffer.check_version(buf)  # Should not raise


def test_version_check_fail():
    buf = SimpleBuffer(x=1.0, y=2.0)
    buf._ipc_version = 99
    with pytest.raises(IncompatibleVersionError):
        SimpleBuffer.check_version(buf)


def test_array_buffer():
    buf = ArrayBuffer()
    buf.data[0] = 42
    buf.data[15] = 255
    data = buf.to_bytes()
    restored = ArrayBuffer.from_bytes(data)
    assert restored.data[0] == 42
    assert restored.data[15] == 255


def test_buffer_from_arrays_basic():
    FrameBuffer = buffer_from_arrays(
        "FrameBuffer",
        version=1,
        bgr=np.zeros((4, 6, 3), dtype=np.uint8),
    )

    buf = FrameBuffer()
    arr = buf.bgr
    assert arr.shape == (4, 6, 3)
    assert arr.dtype == np.uint8

    # Write via numpy view
    arr[:] = 42
    assert buf.bgr[0, 0, 0] == 42


def test_buffer_from_arrays_multiple_fields():
    Multi = buffer_from_arrays(
        "Multi",
        version=3,
        rgb=np.zeros((2, 2, 3), dtype=np.uint8),
        depth=np.zeros((2, 2), dtype=np.float32),
    )

    buf = Multi()
    assert buf.rgb.shape == (2, 2, 3)
    assert buf.depth.shape == (2, 2)
    assert buf._ipc_version == 3


def test_buffer_from_arrays_serialization():
    B = buffer_from_arrays("B", bgr=np.zeros((2, 3), dtype=np.float64))
    buf = B()
    buf.bgr[:] = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]

    data = buf.to_bytes()
    restored = B.from_bytes(data)
    np.testing.assert_array_equal(restored.bgr, buf.bgr)


# --- Annotation DSL tests ---


def test_dsl_scalars():
    class P(Buffer):
        _version_ = 1
        x: float
        y: float
        seq: int
        ok: bool

    p = P(x=1.5, y=2.5, seq=7, ok=True)
    assert p.x == 1.5 and p.y == 2.5 and p.seq == 7 and p.ok is True
    assert p._ipc_version == 1
    # Wire size: 4 (version) + 8 + 8 + 8 + 1, possibly padded.
    # We only assert that it's at least the sum-without-padding.
    assert ctypes.sizeof(P) >= 4 + 8 + 8 + 8 + 1


def test_dsl_ndarray_field():
    class F(Buffer):
        _version_ = 1
        bgr: np.ndarray = np.zeros((4, 6, 3), dtype=np.uint8)

    buf = F()
    assert buf.bgr.shape == (4, 6, 3)
    assert buf.bgr.dtype == np.uint8
    buf.bgr[1, 2, 0] = 99
    assert buf.bgr[1, 2, 0] == 99
    # Class-level default must not leak as a shared mutable.
    other = F()
    assert other.bgr[1, 2, 0] == 0


def test_dsl_mixed_scalars_and_ndarray():
    class S(Buffer):
        _version_ = 2
        timestamp: float
        seq: int
        bgr: np.ndarray = np.zeros((8, 8, 3), dtype=np.uint8)
        intrinsics: np.ndarray = np.zeros((3, 3), dtype=np.float64)

    s = S(timestamp=1.0, seq=42)
    s.bgr[0, 0, 0] = 1
    s.intrinsics[:] = np.eye(3)
    assert s.timestamp == 1.0 and s.seq == 42
    assert s.bgr[0, 0, 0] == 1
    np.testing.assert_array_equal(s.intrinsics, np.eye(3))
    assert s._ipc_version == 2


def test_dsl_scalar_defaults():
    class D(Buffer):
        _version_ = 1
        x: float = 1.5
        y: int = 42

    d = D()
    assert d.x == 1.5 and d.y == 42
    # Per-call override works.
    d2 = D(x=0.5)
    assert d2.x == 0.5 and d2.y == 42  # default still applied for y


def test_dsl_ctypes_scalar_annotation():
    class T(Buffer):
        _version_ = 1
        a: ctypes.c_uint8
        b: ctypes.c_int32 = 7

    t = T(a=200)
    assert t.a == 200 and t.b == 7


def test_dsl_rejects_ndarray_without_default():
    with pytest.raises(TypeError, match="require a class-level default"):
        class Bad(Buffer):
            _version_ = 1
            bgr: np.ndarray


def test_dsl_rejects_mixed_fields_and_annotations():
    with pytest.raises(TypeError, match="cannot mix"):
        class Bad(Buffer):
            _version_ = 1
            x: float
            _fields_ = [("y", ctypes.c_double)]


def test_dsl_rejects_unsupported_annotation():
    with pytest.raises(TypeError, match="unsupported annotation"):
        class Bad(Buffer):
            _version_ = 1
            stuff: list


def test_dsl_serialization_roundtrip():
    """A DSL-defined buffer must round-trip cleanly through to_bytes/from_bytes."""

    class F(Buffer):
        _version_ = 3
        seq: int
        timestamp: float
        bgr: np.ndarray = np.zeros((2, 2, 3), dtype=np.uint8)

    a = F(seq=11, timestamp=3.14)
    a.bgr[0, 0, 0] = 7
    a.bgr[1, 1, 2] = 250
    restored = F.from_bytes(a.to_bytes())
    assert restored.seq == 11
    assert restored.timestamp == pytest.approx(3.14)
    assert restored.bgr[0, 0, 0] == 7
    assert restored.bgr[1, 1, 2] == 250
    assert restored._ipc_version == 3
