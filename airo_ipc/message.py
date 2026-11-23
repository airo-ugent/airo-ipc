"""Message definitions: one class describes the data for every airo-ipc transport.

Define a message by subclassing :class:`Message` and annotating its fields::

    import numpy as np
    from numpy.typing import NDArray
    from airo_ipc import Array, Message, i32

    class CameraFrame(Message):
        stamp: float                                             # 64-bit float
        frame_id: i32                                            # 32-bit signed integer
        rgb: NDArray[np.uint8] = Array((480, 640, 3), np.uint8)  # fixed-shape numpy array

    frame = CameraFrame(stamp=0.5, frame_id=3)
    frame.rgb[:] = image          # numpy view into the message memory: no extra copy

Supported field annotations:

- ``float`` (64-bit), ``int`` (64-bit signed), ``bool``.
- The sized scalar aliases ``i8``, ``i16``, ``i32``, ``i64``, ``u8``, ``u16``, ``u32``, ``u64``,
  ``f32`` and ``f64`` from this module.
- ``NDArray[...]`` with an ``Array(shape, dtype)`` default. The dtype may be any bool, integer,
  float or complex dtype.

When a shape is only known at runtime (for example, a camera resolution), use ``None`` for the
unknown dimensions and create a concrete class with :meth:`Message.with_shapes`::

    class CameraFrame(Message):
        rgb: NDArray[np.uint8] = Array((None, None, 3), np.uint8)

    Frame = CameraFrame.with_shapes(rgb=(720, 1280, 3))

A message is a fixed-size :class:`ctypes.Structure`. Its memory layout is a hidden 24-byte header
(schema fingerprint, sequence number, publish time), followed by the fields in declaration order.
The schema fingerprint is a hash of the class name and of every field name, type and shape. The
transports compare it, so a sender and a receiver with different definitions fail loudly instead
of silently reading garbage.
"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Self,
    get_args,
    get_origin,
)

import numpy as np
from numpy.typing import DTypeLike

from airo_ipc.errors import SchemaMismatchError

if sys.byteorder != "little":  # pragma: no cover - no big-endian CI runner
    raise ImportError(
        "airo-ipc sends messages as raw little-endian memory and does not support big-endian hosts."
    )

if TYPE_CHECKING:
    from _ctypes import _PyCStructType as _StructMeta
else:
    _StructMeta = type(ctypes.Structure)

__all__ = [
    "Array",
    "ArrayField",
    "Message",
    "MessageMeta",
    "ScalarField",
    "WIRE_FORMAT_VERSION",
    "HEADER_SIZE",
    "f32",
    "f64",
    "i8",
    "i16",
    "i32",
    "i64",
    "u8",
    "u16",
    "u32",
    "u64",
]

#: Version of the message memory layout. A change here changes every schema fingerprint.
WIRE_FORMAT_VERSION = 1

_HEADER_FIELDS: list[tuple[str, type[ctypes.c_uint64]]] = [
    ("_airo_schema", ctypes.c_uint64),
    ("_airo_seq", ctypes.c_uint64),
    ("_airo_stamp_ns", ctypes.c_uint64),
]
#: Size in bytes of the hidden header at the start of every message.
HEADER_SIZE = 24

# Sized scalar aliases. Static type checkers see a plain int / float.
i8 = Annotated[int, ctypes.c_int8]
i16 = Annotated[int, ctypes.c_int16]
i32 = Annotated[int, ctypes.c_int32]
i64 = Annotated[int, ctypes.c_int64]
u8 = Annotated[int, ctypes.c_uint8]
u16 = Annotated[int, ctypes.c_uint16]
u32 = Annotated[int, ctypes.c_uint32]
u64 = Annotated[int, ctypes.c_uint64]
f32 = Annotated[float, ctypes.c_float]
f64 = Annotated[float, ctypes.c_double]

_PLAIN_SCALARS: dict[type, type[Any]] = {
    float: ctypes.c_double,
    int: ctypes.c_int64,
    bool: ctypes.c_bool,
}

# ctypes `_type_` code -> scalar kind ("i" signed, "u" unsigned, "f" float, "bool").
_CTYPE_KINDS = {
    "b": "i",
    "h": "i",
    "i": "i",
    "l": "i",
    "q": "i",
    "B": "u",
    "H": "u",
    "I": "u",
    "L": "u",
    "Q": "u",
    "f": "f",
    "d": "f",
    "?": "bool",
}
# Scalar kind -> Python type that a field of that kind holds.
_KIND_PYTYPE: dict[str, type] = {"i": int, "u": int, "f": float, "bool": bool}

# Unsigned ctypes integer used as storage element for arrays, by element alignment.
_STORAGE_BY_ALIGNMENT: dict[int, type[Any]] = {
    1: ctypes.c_uint8,
    2: ctypes.c_uint16,
    4: ctypes.c_uint32,
    8: ctypes.c_uint64,
}

# Namespace keys that are never copied into a class made by `with_shapes`.
_NON_EXTRA_KEYS = frozenset(
    {
        "__module__",
        "__qualname__",
        "__annotations__",
        "__annotate__",
        "__annotate_func__",
        "__annotations_cache__",
        "__dict__",
        "__weakref__",
        "__classcell__",
        "__static_attributes__",
        "__firstlineno__",
    }
)

_Shape = tuple[int | None, ...]
_ResolvedShape = tuple[int, ...]


# --------------------------------------------------------------------------------------------
# Field descriptions
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScalarField:
    """A scalar field of a message."""

    name: str
    ctype: type[Any]
    code: str  # "i32", "u8", "f64", "bool", ...

    def signature(self) -> str:
        return f"{self.name}:s:{self.code}"


@dataclass(frozen=True)
class ArrayField:
    """A fixed-shape numpy array field of a message."""

    name: str
    shape: _Shape
    dtype: np.dtype[Any]

    @property
    def resolved(self) -> bool:
        """True if every dimension is known."""
        return all(d is not None for d in self.shape)

    @property
    def storage_name(self) -> str:
        return f"_airo_arr_{self.name}"

    @property
    def nbytes(self) -> int:
        return math.prod(self._resolved_shape()) * self.dtype.itemsize

    def storage_ctype(self) -> type[Any]:
        """The ctypes array type that holds the raw bytes of this field."""
        element = _STORAGE_BY_ALIGNMENT[min(self.dtype.alignment, 8)]
        return element * (self.nbytes // ctypes.sizeof(element))

    def signature(self) -> str:
        dims = "x".join("?" if d is None else str(d) for d in self.shape)
        return f"{self.name}:a:{self.dtype.str}:{dims}"

    def _resolved_shape(self) -> _ResolvedShape:
        if not self.resolved:
            raise TypeError(
                f"array field '{self.name}' has unresolved shape {self.shape}"
            )
        return tuple(d for d in self.shape if d is not None)


_Field = ScalarField | ArrayField


@dataclass(frozen=True)
class _ArraySpec:
    """The value returned by :func:`Array`; replaced by a descriptor when the class is built."""

    shape: _Shape
    dtype: np.dtype[Any]


@dataclass(frozen=True)
class MessageMeta:
    """Metadata that the transport writes into every published message.

    Attributes:
        seq: Sequence number, assigned by the publisher. Starts at 1; 0 means "never published".
        stamp_ns: Publish time in nanoseconds since the Unix epoch (``time.time_ns()``).
    """

    seq: int
    stamp_ns: int


def _normalize_shape(shape: Sequence[int | None] | int, *, allow_none: bool) -> _Shape:
    dims: tuple[Any, ...] = (shape,) if isinstance(shape, int) else tuple(shape)
    for d in dims:
        if d is None and allow_none:
            continue
        if isinstance(d, bool) or not isinstance(d, (int, np.integer)) or d < 0:
            raise TypeError(
                f"invalid array shape {shape!r}: dimensions must be non-negative integers"
                + (" or None" if allow_none else "")
            )
    return tuple(None if d is None else int(d) for d in dims)


def _normalize_dtype(dtype: DTypeLike) -> np.dtype[Any]:
    dt = np.dtype(dtype)
    if dt.kind not in "biufc" or dt.fields is not None or dt.subdtype is not None:
        raise TypeError(
            f"unsupported array dtype {dt}: use a bool, integer, float or complex dtype"
        )
    if dt.byteorder == ">":
        raise TypeError(
            f"unsupported array dtype {dt}: big-endian dtypes are not supported"
        )
    if dt.alignment not in (1, 2, 4, 8, 16):  # pragma: no cover - exotic platforms only
        raise TypeError(f"unsupported array dtype {dt}: alignment {dt.alignment}")
    return dt


def Array(shape: Sequence[int | None] | int, dtype: DTypeLike) -> Any:  # noqa: N802
    """Declare a fixed-shape numpy array field.

    Use it as the default value of an ``NDArray`` annotation::

        rgb: NDArray[np.uint8] = Array((480, 640, 3), np.uint8)

    Args:
        shape: The array shape. Use ``None`` for a dimension that is only known at runtime, and
            create a concrete class later with :meth:`Message.with_shapes`.
        dtype: A numpy bool, integer, float or complex dtype.

    Returns:
        A placeholder that the :class:`Message` class replaces with a numpy-view descriptor. The
        return type is ``Any`` so that static type checkers accept the ``NDArray`` annotation.
    """
    return _ArraySpec(_normalize_shape(shape, allow_none=True), _normalize_dtype(dtype))


# --------------------------------------------------------------------------------------------
# Class construction
# --------------------------------------------------------------------------------------------


class _ArrayDescriptor:
    """Exposes an array field as a numpy view into the message memory."""

    __slots__ = ("field",)

    def __init__(self, field: ArrayField) -> None:
        self.field = field

    def __get__(self, obj: Message | None, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        f = self.field
        return np.frombuffer(getattr(obj, f.storage_name), dtype=f.dtype).reshape(
            f.shape
        )

    def __set__(self, obj: Message, value: Any) -> None:
        """Copy ``value`` into the field.

        A numpy array must have the field's shape (or be 0-d) and a dtype that casts with numpy's
        ``same_kind`` rule. Python scalars and lists use normal numpy assignment rules, so
        ``msg.rgb = 0`` works for a ``uint8`` field.
        """
        view = self.__get__(obj)
        shape = np.shape(value)
        if shape != () and shape != view.shape:
            raise ValueError(
                f"{type(obj).__name__}.{self.field.name}: cannot assign an array of shape "
                f"{shape}; the field has shape {view.shape}"
            )
        if isinstance(value, np.ndarray):
            np.copyto(view, value, casting="same_kind")
        else:
            view[...] = value


def _scalar_code(ctype: type[Any]) -> str:
    kind = _CTYPE_KINDS[getattr(ctype, "_type_")]
    return "bool" if kind == "bool" else f"{kind}{8 * ctypes.sizeof(ctype)}"


def _scalar_from_annotation(cls_name: str, name: str, annotation: Any) -> ScalarField:
    if annotation in _PLAIN_SCALARS:
        ctype = _PLAIN_SCALARS[annotation]
        return ScalarField(name, ctype, _scalar_code(ctype))
    if get_origin(annotation) is Annotated:
        base, *extras = get_args(annotation)
        ctypes_extras: list[type[Any]] = [
            e
            for e in extras
            if isinstance(e, type) and issubclass(e, ctypes._SimpleCData)
        ]
        if (
            len(ctypes_extras) == 1
            and getattr(ctypes_extras[0], "_type_", "") in _CTYPE_KINDS
        ):
            ctype = ctypes_extras[0]
            kind = _CTYPE_KINDS[getattr(ctype, "_type_")]
            if base is not _KIND_PYTYPE[kind]:
                raise TypeError(
                    f"{cls_name}.{name}: Annotated[{base.__name__}, {ctype.__name__}] mixes "
                    f"incompatible types; use Annotated[{_KIND_PYTYPE[kind].__name__}, ...]"
                )
            return ScalarField(name, ctype, _scalar_code(ctype))
    raise TypeError(
        f"{cls_name}.{name}: unsupported field annotation {annotation!r}. Use float, int, bool, "
        f"a sized alias (i8, i16, i32, i64, u8, u16, u32, u64, f32, f64) or "
        f"NDArray[...] = Array(shape, dtype)."
    )


def _is_ndarray_annotation(annotation: Any) -> bool:
    return annotation is np.ndarray or get_origin(annotation) is np.ndarray


def _check_annotation_dtype(
    cls_name: str, name: str, annotation: Any, spec: _ArraySpec
) -> None:
    """If the annotation names a dtype (``NDArray[np.uint8]``), it must match ``Array(...)``."""
    args = get_args(annotation)
    if len(args) != 2:
        return
    dtype_args = get_args(args[1])
    if len(dtype_args) != 1:
        return
    scalar_type = dtype_args[0]
    if not (isinstance(scalar_type, type) and issubclass(scalar_type, np.generic)):
        return  # Any, TypeVar, ...
    if not issubclass(spec.dtype.type, scalar_type):
        raise TypeError(
            f"{cls_name}.{name}: the annotation says {scalar_type.__name__}, but "
            f"Array(...) says {spec.dtype}"
        )


def _reserved_names() -> frozenset[str]:
    return frozenset(name for name in dir(Message) if not name.startswith("_"))


def _evaluate_annotations(
    name: str, namespace: dict[str, Any], localns: dict[str, Any] | None
) -> dict[str, Any]:
    """Evaluate the annotations of a class body before the class exists.

    ctypes requires `_fields_` in the namespace at class creation, so the annotations must be
    read from the raw namespace. A throw-away plain class lets `inspect.get_annotations` handle
    string annotations (``from __future__ import annotations``) and lazy annotations (3.14+).
    """
    module_name = namespace.get("__module__", "")
    module = sys.modules.get(module_name)
    globalns = dict(vars(module)) if module is not None else {}
    probe_ns = {
        k: v
        for k, v in namespace.items()
        if k.startswith("__annotat") or k in ("__module__", "__qualname__")
    }
    probe = type(name, (), probe_ns)
    locals_ = {**(localns or {}), **namespace}
    try:
        return dict(
            inspect.get_annotations(
                probe, globals=globalns, locals=locals_, eval_str=True
            )
        )
    except NameError as e:
        raise TypeError(
            f"{name}: cannot resolve a field annotation ({e}). Import every name that the "
            f"annotations use."
        ) from e


def _parse_fields(
    cls_name: str, namespace: dict[str, Any], annotations: dict[str, Any]
) -> tuple[tuple[_Field, ...], dict[str, Any]]:
    fields: list[_Field] = []
    defaults: dict[str, Any] = {}
    reserved = _reserved_names()
    missing = object()
    for name, annotation in annotations.items():
        if annotation is ClassVar or get_origin(annotation) is ClassVar:
            continue
        if name.startswith("_"):
            raise TypeError(
                f"{cls_name}.{name}: field names must not start with '_' (reserved for "
                f"airo-ipc and ctypes)"
            )
        if name in reserved:
            raise TypeError(f"{cls_name}.{name}: '{name}' is reserved by Message")
        default = namespace.get(name, missing)
        if _is_ndarray_annotation(annotation):
            if not isinstance(default, _ArraySpec):
                raise TypeError(
                    f"{cls_name}.{name}: an NDArray field needs a shape and dtype, for "
                    f"example `{name}: NDArray[np.uint8] = Array((480, 640, 3), np.uint8)`"
                )
            _check_annotation_dtype(cls_name, name, annotation, default)
            fields.append(ArrayField(name, default.shape, default.dtype))
            continue
        if isinstance(default, _ArraySpec):
            raise TypeError(
                f"{cls_name}.{name}: Array(...) needs an NDArray annotation, for example "
                f"`{name}: NDArray[np.uint8] = Array(...)`"
            )
        field = _scalar_from_annotation(cls_name, name, annotation)
        fields.append(field)
        if default is not missing:
            pytype = _KIND_PYTYPE[_CTYPE_KINDS[getattr(field.ctype, "_type_")]]
            if pytype is float:
                ok = isinstance(default, (int, float)) and not isinstance(default, bool)
            elif pytype is int:
                ok = isinstance(default, int) and not isinstance(default, bool)
            else:
                ok = isinstance(default, bool)
            if not ok:
                raise TypeError(
                    f"{cls_name}.{name}: default {default!r} is not a {pytype.__name__}"
                )
            defaults[name] = default

    field_names = {f.name for f in fields}
    for key, value in namespace.items():
        if isinstance(value, _ArraySpec) and key not in field_names:
            raise TypeError(
                f"{cls_name}.{key}: Array(...) needs an NDArray annotation, for example "
                f"`{key}: NDArray[np.uint8] = Array(...)`"
            )
    return tuple(fields), defaults


def _fingerprint(name: str, fields: Sequence[_Field]) -> int:
    canonical = ";".join(
        [f"airo_ipc/wire{WIRE_FORMAT_VERSION}", name, *(f.signature() for f in fields)]
    )
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def _build_namespace(
    name: str,
    namespace: dict[str, Any],
    fields: tuple[_Field, ...],
    defaults: dict[str, Any],
    extras: dict[str, Any],
    template: type[Message] | None,
) -> dict[str, Any]:
    """Return the class namespace with `_fields_`, array descriptors and airo-ipc metadata."""
    ns = {k: v for k, v in namespace.items() if k not in {f.name for f in fields}}

    ctypes_fields: list[tuple[str, type[Any]]] = list(_HEADER_FIELDS)
    for f in fields:
        if isinstance(f, ScalarField):
            ctypes_fields.append((f.name, f.ctype))
        elif f.resolved:
            ctypes_fields.append((f.storage_name, f.storage_ctype()))
    ns["_fields_"] = ctypes_fields

    for f in fields:
        if isinstance(f, ArrayField):
            ns[f.name] = _ArrayDescriptor(f)

    abstract = any(isinstance(f, ArrayField) and not f.resolved for f in fields)
    ns["_airo_fields_"] = fields
    ns["_airo_field_names_"] = frozenset(f.name for f in fields)
    ns["_airo_defaults_"] = dict(defaults)
    ns["_airo_extras_"] = dict(extras)
    ns["_airo_abstract_"] = abstract
    ns["_airo_template_"] = template
    ns["_airo_shape_cache_"] = {}
    ns["_airo_fingerprint_"] = 0 if abstract else _fingerprint(name, fields)
    return ns


class _MessageMeta(_StructMeta):
    """Builds the ctypes layout of a :class:`Message` subclass from its annotations."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> _MessageMeta:
        if namespace.get("_airo_root_", False):
            return super().__new__(mcs, name, bases, namespace, **kwargs)

        prebuilt = namespace.pop("_airo_prebuilt_", None)
        if "_fields_" in namespace:
            raise TypeError(
                f"{name}: do not set _fields_ on a Message; declare fields with annotations"
            )
        for base in bases:
            if isinstance(base, _MessageMeta) and not base.__dict__.get(
                "_airo_root_", False
            ):
                raise TypeError(
                    f"{name}: a message class cannot inherit from another message class "
                    f"({base.__name__}). Subclass Message directly. For runtime shapes, use "
                    f"{base.__name__}.with_shapes(...)."
                )

        if prebuilt is None:
            # The frame that executes the `class` statement: needed to resolve string
            # annotations (`from __future__ import annotations`) that name local variables.
            try:
                localns: dict[str, Any] | None = dict(sys._getframe(1).f_locals)
            except (AttributeError, ValueError):  # pragma: no cover - non-CPython
                localns = None
            annotations = _evaluate_annotations(name, namespace, localns)
            fields, defaults = _parse_fields(name, namespace, annotations)
            field_names = {f.name for f in fields}
            extras = {
                k: v
                for k, v in namespace.items()
                if k not in _NON_EXTRA_KEYS and k not in field_names
            }
            template: type[Message] | None = None
        else:
            fields, defaults, extras, template = prebuilt

        ns = _build_namespace(name, namespace, fields, defaults, extras, template)
        return super().__new__(mcs, name, bases, ns, **kwargs)


# --------------------------------------------------------------------------------------------
# Message base class
# --------------------------------------------------------------------------------------------


class Message(ctypes.Structure, metaclass=_MessageMeta):
    """Base class for every airo-ipc message. See the module docstring for how to define one."""

    _airo_root_: ClassVar[bool] = True
    _airo_fields_: ClassVar[tuple[_Field, ...]] = ()
    _airo_field_names_: ClassVar[frozenset[str]] = frozenset()
    _airo_defaults_: ClassVar[dict[str, Any]] = {}
    _airo_extras_: ClassVar[dict[str, Any]] = {}
    _airo_abstract_: ClassVar[bool] = True
    _airo_template_: ClassVar[type[Message] | None] = None
    _airo_shape_cache_: ClassVar[
        dict[tuple[tuple[str, _Shape], ...], type[Message]]
    ] = {}
    _airo_fingerprint_: ClassVar[int] = 0

    def __init__(self, **fields: Any) -> None:
        cls = type(self)
        if cls._airo_abstract_:
            raise TypeError(cls._abstract_error())
        super().__init__()
        unknown = set(fields) - cls._airo_field_names_
        if unknown:
            raise TypeError(
                f"{cls.__name__}: unknown field(s) {sorted(unknown)}; "
                f"fields are {list(cls.field_names())}"
            )
        for name, value in {**cls._airo_defaults_, **fields}.items():
            setattr(self, name, value)
        self._airo_schema = cls._airo_fingerprint_

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_") and name not in type(self)._airo_field_names_:
            raise AttributeError(
                f"{type(self).__name__} has no field '{name}'; "
                f"fields are {list(type(self).field_names())}"
            )
        super().__setattr__(name, value)

    # -- schema ---------------------------------------------------------------------------

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """The field names, in declaration order."""
        return tuple(f.name for f in cls._airo_fields_)

    @classmethod
    def schema_fingerprint(cls) -> int:
        """The 64-bit schema fingerprint that the transports compare."""
        cls._require_concrete()
        return cls._airo_fingerprint_

    @classmethod
    def type_name(cls) -> str:
        """The type name used by iceoryx2 to check that both sides use the same type."""
        cls._require_concrete()
        return f"airo_ipc::{cls.__name__}::{cls._airo_fingerprint_:016x}"

    @classmethod
    def describe_schema(cls) -> str:
        """A human-readable description of the fields, for debugging schema mismatches."""
        lines = [f"{cls.__name__} ({ctypes.sizeof(cls)} bytes)"]
        for f in cls._airo_fields_:
            if isinstance(f, ScalarField):
                lines.append(f"  {f.name}: {f.code}")
            else:
                lines.append(f"  {f.name}: {f.dtype} {f.shape}")
        return "\n".join(lines)

    @classmethod
    def with_shapes(cls, **shapes: Sequence[int]) -> type[Self]:
        """Return a concrete message class with the given array shapes.

        Use it when a shape is only known at runtime::

            Frame = CameraFrame.with_shapes(rgb=(h, w, 3), depth=(h, w))

        Every ``None`` dimension in the class definition must get a value; fixed dimensions must
        match. Calls with the same shapes return the same class. The result is a sibling class of
        ``cls`` (it is not a subclass), with the same name, methods and defaults.
        """
        template: type[Message] = cls._airo_template_ or cls
        arrays = {
            f.name: f for f in template._airo_fields_ if isinstance(f, ArrayField)
        }
        unknown = set(shapes) - set(arrays)
        if unknown:
            raise TypeError(
                f"{template.__name__}.with_shapes: unknown array field(s) {sorted(unknown)}; "
                f"array fields are {sorted(arrays)}"
            )
        new_fields: list[_Field] = []
        for f in template._airo_fields_:
            if isinstance(f, ArrayField) and f.name in shapes:
                new_shape = _normalize_shape(shapes[f.name], allow_none=False)
                if len(new_shape) != len(f.shape) or any(
                    t is not None and t != n for t, n in zip(f.shape, new_shape)
                ):
                    raise ValueError(
                        f"{template.__name__}.{f.name}: shape {new_shape} does not match the "
                        f"declared shape {f.shape}"
                    )
                f = ArrayField(f.name, new_shape, f.dtype)
            new_fields.append(f)
        unresolved = [
            f.name for f in new_fields if isinstance(f, ArrayField) and not f.resolved
        ]
        if unresolved:
            raise TypeError(
                f"{template.__name__}.with_shapes: give a shape for {sorted(unresolved)}"
            )

        key = tuple((f.name, f.shape) for f in new_fields if isinstance(f, ArrayField))
        template_key = tuple(
            (f.name, f.shape)
            for f in template._airo_fields_
            if isinstance(f, ArrayField)
        )
        if key == template_key and not template._airo_abstract_:
            return template  # type: ignore[return-value]
        cached = template._airo_shape_cache_.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        dims = ", ".join("x".join(str(d) for d in shape) for _, shape in key)
        namespace = dict(template._airo_extras_)
        namespace["__module__"] = template.__module__
        namespace["__qualname__"] = f"{template.__qualname__}[{dims}]"
        namespace["_airo_prebuilt_"] = (
            tuple(new_fields),
            dict(template._airo_defaults_),
            dict(template._airo_extras_),
            template,
        )
        new_cls = _MessageMeta(template.__name__, template.__bases__, namespace)
        template._airo_shape_cache_[key] = new_cls  # type: ignore[assignment]
        return new_cls  # type: ignore[return-value]

    @classmethod
    def _abstract_error(cls) -> str:
        unresolved = [
            f"{f.name}={f.shape}"
            for f in cls._airo_fields_
            if isinstance(f, ArrayField) and not f.resolved
        ]
        if not unresolved:
            return "Message is an abstract base class; subclass it to define a message"
        return (
            f"{cls.__name__} has array fields with unknown dimensions ({', '.join(unresolved)}); "
            f"create a concrete class with {cls.__name__}.with_shapes(...)"
        )

    @classmethod
    def _require_concrete(cls) -> None:
        if cls._airo_abstract_:
            raise TypeError(cls._abstract_error())

    # -- header ---------------------------------------------------------------------------

    @property
    def meta(self) -> MessageMeta:
        """The sequence number and publish time written by the transport."""
        return MessageMeta(seq=int(self._airo_seq), stamp_ns=int(self._airo_stamp_ns))

    def _airo_set_meta(self, seq: int, stamp_ns: int) -> None:
        """Write the header. Called by the transports right before sending."""
        self._airo_schema = type(self)._airo_fingerprint_
        self._airo_seq = seq
        self._airo_stamp_ns = stamp_ns

    def check_schema(self) -> None:
        """Raise :class:`SchemaMismatchError` if the header does not match this class."""
        cls = type(self)
        received = int(self._airo_schema)
        if received != cls._airo_fingerprint_:
            raise SchemaMismatchError(_mismatch_message(cls, received))

    # -- bytes ----------------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """The raw message memory, header included. This is the network wire format."""
        return bytes(memoryview(self).cast("B"))

    @classmethod
    def from_bytes(
        cls, data: bytes | bytearray | memoryview, *, copy: bool = True
    ) -> Self:
        """Build a message from its wire format, and check the schema fingerprint.

        Args:
            data: Exactly ``ctypes.sizeof(cls)`` bytes.
            copy: If False, the message shares memory with ``data``, which must be writable
                (for example a ``bytearray``).

        Raises:
            ValueError: ``data`` has the wrong size.
            SchemaMismatchError: the sender used a different message definition.
        """
        cls._require_concrete()
        view = memoryview(data).cast("B")
        size = ctypes.sizeof(cls)
        if view.nbytes != size:
            raise ValueError(
                f"{cls.__name__}: expected {size} bytes, got {view.nbytes}. The sender probably "
                f"uses a different message definition."
            )
        received = int.from_bytes(view[:8], "little")
        if received != cls._airo_fingerprint_:
            raise SchemaMismatchError(_mismatch_message(cls, received))
        return cls.from_buffer_copy(view) if copy else cls.from_buffer(view)

    def copy(self) -> Self:
        """An independent copy that owns its memory."""
        return type(self).from_buffer_copy(self)

    # -- comparison and display -----------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        for f in type(self)._airo_fields_:
            a, b = getattr(self, f.name), getattr(other, f.name)
            if isinstance(f, ArrayField):
                if not np.array_equal(a, b):
                    return False
            elif a != b:
                return False
        return True

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        parts = []
        for f in type(self)._airo_fields_:
            if isinstance(f, ArrayField):
                parts.append(f"{f.name}=<{f.dtype} {f.shape}>")
            else:
                parts.append(f"{f.name}={getattr(self, f.name)!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


def _mismatch_message(cls: type[Message], received: int) -> str:
    return (
        f"received a message with schema fingerprint {received:016x}, but {cls.__name__} expects "
        f"{cls._airo_fingerprint_:016x}. The sender uses a different definition of this message "
        f"(class name, field names, types, shapes or order differ). Use the same definition on "
        f"both sides.\nLocal definition:\n{cls.describe_schema()}"
    )
