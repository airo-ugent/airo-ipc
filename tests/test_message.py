"""Tests for airo_ipc.message.

This module uses `from __future__ import annotations`, so every annotation reaches the Message
metaclass as a string. `test_message_eager.py` covers evaluated annotations.
"""

from __future__ import annotations

import ctypes
import uuid

import numpy as np
import pytest
from numpy.typing import NDArray

from airo_ipc.errors import LoanReleasedError, ReadOnlyMessageError, SchemaMismatchError
from airo_ipc.message import (
    HEADER_SIZE,
    Array,
    Message,
    MessageMeta,
    f32,
    i8,
    i32,
    u16,
    u64,
)


class Point(Message):
    x: float
    y: float = 2.5
    label: i32 = 7
    valid: bool = True


class CameraFrame(Message):
    stamp: float
    frame_id: u64
    rgb: NDArray[np.uint8] = Array((4, 5, 3), np.uint8)
    depth: NDArray[np.float32] = Array((4, 5), np.float32)


class Template(Message):
    stamp: float = 1.0
    rgb: NDArray[np.uint8] = Array((None, None, 3), np.uint8)
    depth: NDArray[np.uint16] = Array((None, None), np.uint16)

    def pixel_count(self) -> int:
        return int(self.rgb.shape[0] * self.rgb.shape[1])


# -- layout -------------------------------------------------------------------------------------


def test_header_is_first_32_bytes() -> None:
    assert HEADER_SIZE == 32
    assert Point._airo_schema.offset == 0  # type: ignore[attr-defined]
    assert Point._airo_seq.offset == 8  # type: ignore[attr-defined]
    assert Point._airo_stamp_ns.offset == 16  # type: ignore[attr-defined]
    assert Point._airo_flags.offset == 24  # type: ignore[attr-defined]
    assert Point.x.offset == HEADER_SIZE  # type: ignore[attr-defined]


def test_fields_follow_declaration_order() -> None:
    assert Point.field_names() == ("x", "y", "label", "valid")
    offsets = [getattr(Point, n).offset for n in ("x", "y", "label", "valid")]
    assert offsets == sorted(offsets)


def test_size_includes_arrays_and_padding() -> None:
    # 32 header + 8 stamp + 8 frame_id + 60 rgb + 4 padding (float32 alignment) + 80 depth
    assert ctypes.sizeof(CameraFrame) == 32 + 8 + 8 + 60 + 4 + 80


def test_sized_aliases_map_to_ctypes() -> None:
    class Sized(Message):
        a: i8
        b: u16
        c: f32

    assert Sized.describe_schema().splitlines()[1:] == [
        "  a: i8",
        "  b: u16",
        "  c: f32",
    ]
    s = Sized(a=-3, b=65535, c=0.5)
    assert (s.a, s.b, s.c) == (-3, 65535, 0.5)


# -- construction -------------------------------------------------------------------------------


def test_defaults_and_keyword_fields() -> None:
    p = Point(x=1.0)
    assert (p.x, p.y, p.label, p.valid) == (1.0, 2.5, 7, True)
    p = Point(x=1.0, y=-1.0, label=3, valid=False)
    assert (p.x, p.y, p.label, p.valid) == (1.0, -1.0, 3, False)


def test_unknown_field_rejected() -> None:
    with pytest.raises(TypeError, match="unknown field"):
        Point(x=1.0, z=3.0)


def test_positional_arguments_rejected() -> None:
    with pytest.raises(TypeError):
        Point(1.0)  # type: ignore[call-arg]


def test_assigning_unknown_attribute_rejected() -> None:
    p = Point(x=1.0)
    with pytest.raises(AttributeError, match="no field 'xx'"):
        p.xx = 3.0


def test_array_fields_are_writable_views() -> None:
    f = CameraFrame(stamp=0.0, frame_id=1)
    assert f.rgb.shape == (4, 5, 3) and f.rgb.dtype == np.uint8
    assert f.rgb.flags.writeable
    f.rgb[1, 2, 0] = 200
    # The write lands in the message memory itself.
    raw = np.frombuffer(f.to_bytes(), dtype=np.uint8)
    rgb_offset = CameraFrame._airo_arr_rgb.offset  # type: ignore[attr-defined]
    assert raw[rgb_offset + (1 * 5 + 2) * 3] == 200


def test_array_assignment_copies_and_checks_shape() -> None:
    f = CameraFrame(stamp=0.0, frame_id=1)
    src = np.full((4, 5), 1.5, dtype=np.float32)
    f.depth = src
    src[:] = 0
    assert np.all(f.depth == 1.5)
    f.rgb = 3  # scalar broadcast
    assert np.all(f.rgb == 3)
    with pytest.raises(ValueError, match="shape"):
        f.depth = np.zeros((5, 4), dtype=np.float32)
    with pytest.raises(TypeError):
        f.rgb = np.zeros((4, 5, 3), dtype=np.float64)  # float -> uint8 is not same_kind


def test_array_constructor_keyword() -> None:
    f = CameraFrame(stamp=0.0, frame_id=1, depth=np.ones((4, 5), dtype=np.float32))
    assert np.all(f.depth == 1)


def test_view_keeps_message_alive() -> None:
    view = CameraFrame(stamp=0.0, frame_id=1, rgb=9).rgb
    import gc

    gc.collect()
    assert np.all(view == 9)


def test_copy_is_independent() -> None:
    f = CameraFrame(stamp=1.0, frame_id=2, rgb=5)
    g = f.copy()
    g.rgb[:] = 6
    g.stamp = 9.0
    assert np.all(f.rgb == 5) and f.stamp == 1.0
    assert type(g) is CameraFrame


def test_equality_ignores_header() -> None:
    a = CameraFrame(stamp=1.0, frame_id=2, rgb=5)
    b = a.copy()
    b._airo_set_meta(seq=10, stamp_ns=123)
    assert a == b
    b.rgb[0, 0, 0] = 0
    assert a != b
    assert Point(x=1.0) != CameraFrame(stamp=1.0, frame_id=1)


def test_repr() -> None:
    assert repr(Point(x=1.0)) == "Point(x=1.0, y=2.5, label=7, valid=True)"
    assert "rgb=<uint8 (4, 5, 3)>" in repr(CameraFrame(stamp=0.0, frame_id=0))


def test_meta() -> None:
    p = Point(x=1.0)
    assert p.meta == MessageMeta(seq=0, stamp_ns=0)
    p._airo_set_meta(seq=5, stamp_ns=1_700_000_000_000_000_000)
    assert p.meta == MessageMeta(seq=5, stamp_ns=1_700_000_000_000_000_000)


# -- schema fingerprint ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_releases() -> None:
    # If this test fails, the wire format changed: bump WIRE_FORMAT_VERSION on purpose,
    # never by accident. Old and new releases can then not talk to each other.
    assert f"{Point.schema_fingerprint():016x}" == EXPECTED_POINT_FINGERPRINT


EXPECTED_POINT_FINGERPRINT = "07f10e55dc1d7760"


def _fingerprint_of(source: str) -> int:
    namespace: dict[str, object] = {}
    exec(  # noqa: S102 - test helper
        "import numpy as np\n"
        "from numpy.typing import NDArray\n"
        "from airo_ipc.message import Message, Array, i32, f32\n" + source,
        namespace,
    )
    cls = namespace["M"]
    assert isinstance(cls, type) and issubclass(cls, Message)
    return cls.schema_fingerprint()


BASE = "class M(Message):\n    a: float\n    b: NDArray[np.uint8] = Array((2, 3), np.uint8)\n"


@pytest.mark.parametrize(
    "variant",
    [
        "class M(Message):\n    z: float\n    b: NDArray[np.uint8] = Array((2, 3), np.uint8)\n",
        "class M(Message):\n    a: f32\n    b: NDArray[np.uint8] = Array((2, 3), np.uint8)\n",
        "class M(Message):\n    a: float\n    b: NDArray[np.uint16] = Array((2, 3), np.uint16)\n",
        "class M(Message):\n    a: float\n    b: NDArray[np.uint8] = Array((3, 2), np.uint8)\n",
        "class M(Message):\n    b: NDArray[np.uint8] = Array((2, 3), np.uint8)\n    a: float\n",
        "class N(Message):\n    a: float\n    b: NDArray[np.uint8] = Array((2, 3), np.uint8)\n"
        "M = N\n",
    ],
    ids=["field-name", "scalar-type", "dtype", "shape", "order", "class-name"],
)
def test_fingerprint_changes_with_schema(variant: str) -> None:
    assert _fingerprint_of(variant) != _fingerprint_of(BASE)


def test_fingerprint_ignores_defaults_and_methods() -> None:
    variant = (
        BASE.replace("a: float", "a: float = 3.0")
        + "    def f(self) -> int:\n        return 1\n"
    )
    assert _fingerprint_of(variant) == _fingerprint_of(BASE)


def test_type_name_contains_fingerprint() -> None:
    assert Point.type_name() == f"airo_ipc::Point::{Point.schema_fingerprint():016x}"


# -- wire format ----------------------------------------------------------------------------------


def test_bytes_roundtrip() -> None:
    f = CameraFrame(stamp=3.25, frame_id=99, rgb=7)
    f.depth[2, 3] = 4.5
    f._airo_set_meta(seq=3, stamp_ns=42)
    g = CameraFrame.from_bytes(f.to_bytes())
    assert g == f and g.meta == MessageMeta(seq=3, stamp_ns=42)
    assert len(f.to_bytes()) == ctypes.sizeof(CameraFrame)


def test_from_bytes_without_copy_shares_memory() -> None:
    buf = bytearray(Point(x=1.0).to_bytes())
    p = Point.from_bytes(buf, copy=False)
    p.x = 8.0
    assert Point.from_bytes(bytes(buf)).x == 8.0


def test_from_bytes_wrong_size() -> None:
    with pytest.raises(SchemaMismatchError, match="expected"):
        Point.from_bytes(b"\x00" * 3)


def test_from_bytes_schema_mismatch() -> None:
    class Other(Message):
        a: float
        b: float
        c: i32
        d: bool

    assert ctypes.sizeof(Other) == ctypes.sizeof(Point)
    with pytest.raises(SchemaMismatchError, match="Point expects"):
        Point.from_bytes(Other(a=1.0, b=2.0, c=3, d=True).to_bytes())


def test_check_schema() -> None:
    p = Point(x=1.0)
    p.check_schema()
    p._airo_schema = 1
    with pytest.raises(SchemaMismatchError):
        p.check_schema()


# -- runtime shapes -------------------------------------------------------------------------------


def test_template_is_abstract() -> None:
    with pytest.raises(TypeError, match="with_shapes"):
        Template()
    with pytest.raises(TypeError, match="with_shapes"):
        Template.type_name()


def test_with_shapes_builds_concrete_class() -> None:
    Frame = Template.with_shapes(rgb=(6, 8, 3), depth=(6, 8))
    f = Frame()
    assert f.rgb.shape == (6, 8, 3) and f.depth.shape == (6, 8)
    assert f.stamp == 1.0  # defaults are kept
    assert f.pixel_count() == 48  # methods are kept
    assert Frame.__name__ == "Template"
    assert Frame.__qualname__ == "Template[6x8x3, 6x8]"


def test_with_shapes_is_cached() -> None:
    a = Template.with_shapes(rgb=(2, 2, 3), depth=(2, 2))
    assert Template.with_shapes(rgb=(2, 2, 3), depth=(2, 2)) is a
    assert a.with_shapes(rgb=(2, 2, 3), depth=(2, 2)) is a
    assert Template.with_shapes(rgb=(3, 2, 3), depth=(3, 2)) is not a


def test_with_shapes_fingerprint_depends_on_shape() -> None:
    a = Template.with_shapes(rgb=(2, 2, 3), depth=(2, 2))
    b = Template.with_shapes(rgb=(4, 4, 3), depth=(4, 4))
    assert a.schema_fingerprint() != b.schema_fingerprint()


def test_with_shapes_errors() -> None:
    with pytest.raises(ValueError, match="does not match"):
        Template.with_shapes(rgb=(2, 2, 4), depth=(2, 2))
    with pytest.raises(ValueError, match="does not match"):
        Template.with_shapes(rgb=(2, 2), depth=(2, 2))
    with pytest.raises(TypeError, match="give a shape"):
        Template.with_shapes(rgb=(2, 2, 3))
    with pytest.raises(TypeError, match="unknown array field"):
        Template.with_shapes(rgb=(2, 2, 3), depth=(2, 2), mask=(2, 2))
    with pytest.raises(TypeError, match="non-negative"):
        Template.with_shapes(rgb=(2, -1, 3), depth=(2, 2))


def test_with_shapes_on_concrete_class() -> None:
    # Fixed dimensions cannot change; only `None` dimensions are filled in.
    assert CameraFrame.with_shapes(rgb=(4, 5, 3)) is CameraFrame
    with pytest.raises(ValueError, match="does not match"):
        CameraFrame.with_shapes(rgb=(8, 10, 3), depth=(8, 10))


def test_local_alias_in_string_annotation() -> None:
    Image = NDArray[
        np.uint8
    ]  # a local name: needs the class-definition frame to resolve

    class Local(Message):
        img: Image = Array((2, 2), np.uint8)

    assert Local().img.shape == (2, 2)


# -- invalid definitions --------------------------------------------------------------------------


def test_reject_explicit_fields() -> None:
    with pytest.raises(TypeError, match="_fields_"):

        class Bad(Message):
            _fields_ = [("x", ctypes.c_double)]


def test_reject_message_inheritance() -> None:
    with pytest.raises(TypeError, match="cannot inherit"):

        class Bad(Point):
            z: float


def test_reject_private_field() -> None:
    with pytest.raises(TypeError, match="must not start with '_'"):

        class Bad(Message):
            _x: float


def test_reject_reserved_name() -> None:
    with pytest.raises(TypeError, match="reserved"):

        class Bad(Message):
            meta: float


def test_reject_unsupported_annotation() -> None:
    with pytest.raises(TypeError, match="unsupported field annotation"):

        class Bad(Message):
            name: str


def test_reject_raw_ctypes_annotation() -> None:
    with pytest.raises(TypeError, match="sized alias"):

        class Bad(Message):
            x: ctypes.c_int32


def test_reject_ndarray_without_array() -> None:
    with pytest.raises(TypeError, match="needs a shape and dtype"):

        class Bad(Message):
            img: NDArray[np.uint8]


def test_reject_array_without_ndarray_annotation() -> None:
    with pytest.raises(TypeError, match="needs an NDArray annotation"):

        class Bad(Message):
            img: float = Array((2,), np.float64)


def test_reject_annotation_dtype_mismatch() -> None:
    with pytest.raises(TypeError, match="annotation says"):

        class Bad(Message):
            img: NDArray[np.uint8] = Array((2,), np.uint16)


def test_accept_generic_annotation_dtype() -> None:
    class Ok(Message):
        a: NDArray[np.floating] = Array((2,), np.float32)  # type: ignore[type-arg]
        b: np.ndarray = Array((2,), np.int16)  # type: ignore[type-arg]

    assert Ok().a.dtype == np.float32 and Ok().b.dtype == np.int16


@pytest.mark.parametrize("dtype", [object, "U4", ">f4", [("a", "f4")]])
def test_reject_unsupported_dtype(dtype: object) -> None:
    with pytest.raises(TypeError, match="unsupported array dtype"):
        Array((2,), dtype)  # type: ignore[arg-type]


def test_reject_bad_scalar_default() -> None:
    with pytest.raises(TypeError, match="is not a int"):

        class Bad(Message):
            n: int = 1.5  # type: ignore[assignment]


def test_reject_annotated_kind_mismatch() -> None:
    from typing import Annotated

    with pytest.raises(TypeError, match="incompatible"):

        class Bad(Message):
            n: Annotated[float, ctypes.c_int32]


def test_message_base_not_instantiable() -> None:
    with pytest.raises(TypeError, match="abstract"):
        Message()


# -- iceoryx2 integration ---------------------------------------------------------------------


def test_iceoryx2_accepts_message_and_rejects_other_shape() -> None:
    iox2 = pytest.importorskip("iceoryx2")
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    name = iox2.ServiceName.new(f"airo_ipc_test/message/{uuid.uuid4().hex[:8]}")
    Small = Template.with_shapes(rgb=(2, 2, 3), depth=(2, 2))
    Big = Template.with_shapes(rgb=(4, 4, 3), depth=(4, 4))
    assert iox2.get_type_name(Small) == Small.type_name()

    service = node.service_builder(name).publish_subscribe(Small).open_or_create()
    publisher = service.publisher_builder().create()
    subscriber = service.subscriber_builder().create()

    sample = publisher.loan_uninit()
    msg = sample.payload().contents
    assert isinstance(msg, Small)
    msg.rgb[:] = 11  # zero-copy write through the Array descriptor
    msg._airo_set_meta(seq=1, stamp_ns=5)
    sample.assume_init().send()

    received = subscriber.receive().payload().contents
    received.check_schema()
    assert np.all(received.rgb == 11) and received.meta.seq == 1

    with pytest.raises(Exception, match="IncompatibleTypes"):
        node.service_builder(name).publish_subscribe(Big).open_or_create()


# -- loan guards ------------------------------------------------------------------------------


def test_publisher_loan_views_become_read_only() -> None:
    f = CameraFrame(stamp=0.0, frame_id=1)
    f._airo_begin_loan(readonly=False)
    view = f.rgb
    view[:] = 4
    f.stamp = 2.0
    assert f._airo_end_loan() is True  # `view` is still alive
    assert not view.flags.writeable
    with pytest.raises(ValueError):
        view[0, 0, 0] = 1
    with pytest.raises(LoanReleasedError):
        _ = f.rgb
    with pytest.raises(LoanReleasedError):
        f.stamp = 3.0
    with pytest.raises(LoanReleasedError):
        f.depth = 0


def test_readonly_loan() -> None:
    f = CameraFrame(stamp=1.0, frame_id=1, rgb=8)
    f._airo_begin_loan(readonly=True)
    assert f.stamp == 1.0 and np.all(f.rgb == 8)
    assert not f.rgb.flags.writeable
    with pytest.raises(ReadOnlyMessageError):
        f.stamp = 2.0
    with pytest.raises(ReadOnlyMessageError):
        f.rgb = 0
    c = f.copy()  # a copy is a normal, writable message
    c.rgb[:] = 1
    c.stamp = 5.0


def test_end_loan_reports_no_escape_when_views_are_gone() -> None:
    f = CameraFrame(stamp=0.0, frame_id=1)
    f._airo_begin_loan(readonly=True)
    total = int(f.rgb.sum())  # temporary view, dropped right away
    assert total == 0
    assert f._airo_end_loan() is False


def test_set_meta_writes_flags() -> None:
    p = Point(x=1.0)
    p._airo_set_meta(seq=1, stamp_ns=2, flags=1)
    assert p._airo_flags == 1
    assert Point.from_bytes(p.to_bytes())._airo_flags == 1
