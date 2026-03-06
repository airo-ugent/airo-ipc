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
