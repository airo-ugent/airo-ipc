"""Buffer base class for IPC data transport.

Two ways to define a Buffer:

1. **Annotation DSL (recommended)** — dataclass-shaped class body:

       class Point(Buffer):
           _version_ = 1
           x: float
           y: float
           seq: int = 0                                    # scalar default
           bgr: np.ndarray = np.zeros((480, 640, 3), dtype=np.uint8)   # array template

2. **Explicit `_fields_` (still supported)** — direct ctypes form:

       class Point(Buffer):
           _version_ = 1
           _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

Use the DSL for static shapes (compile-time known). Use `buffer_from_arrays(...)` if
you only know the shape at runtime (e.g. from a camera resolution query).
"""

from __future__ import annotations

import ctypes
from typing import Any, Self

import numpy as np


class IncompatibleVersionError(Exception):
    pass


# Mapping from Python primitive annotation to ctypes type.
# `int` defaults to int64 (conservative); use `ctypes.c_int32` etc. for narrower widths.
_PYTHON_TO_CTYPES: dict[type, type] = {
    float: ctypes.c_double,
    int: ctypes.c_int64,
    bool: ctypes.c_bool,
}


# Mapping from numpy dtype to ctypes scalar.
_NP_TO_CTYPES = {
    np.dtype(np.uint8): ctypes.c_uint8,
    np.dtype(np.uint16): ctypes.c_uint16,
    np.dtype(np.uint32): ctypes.c_uint32,
    np.dtype(np.uint64): ctypes.c_uint64,
    np.dtype(np.int8): ctypes.c_int8,
    np.dtype(np.int16): ctypes.c_int16,
    np.dtype(np.int32): ctypes.c_int32,
    np.dtype(np.int64): ctypes.c_int64,
    np.dtype(np.float32): ctypes.c_float,
    np.dtype(np.float64): ctypes.c_double,
    np.dtype(np.bool_): ctypes.c_bool,
}


def _make_ctypes_array(dtype: np.dtype, shape: tuple[int, ...]):
    """Build a nested ctypes Array type matching a numpy shape+dtype."""
    base = _NP_TO_CTYPES.get(dtype)
    if base is None:
        raise TypeError(f"Unsupported numpy dtype: {dtype}")
    ct = base
    for dim in reversed(shape):
        ct = ct * dim
    return ct


def _is_ctypes_scalar(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, ctypes._SimpleCData)


def _make_array_property(storage_name: str, shape: tuple[int, ...], dtype: np.dtype):
    """Return a property that exposes a numpy view over a ctypes array field."""
    def getter(self):
        raw = getattr(self, storage_name)
        return np.frombuffer(raw, dtype=dtype).reshape(shape)
    return property(getter)


class _BufferMeta(type(ctypes.Structure)):
    """Metaclass for Buffer subclasses.

    Handles two definition styles:

    - **Annotation DSL**: scans `__annotations__`, builds `_fields_` from the type
      annotations and class-level defaults, and synthesizes numpy-view properties
      for ndarray fields.
    - **Explicit `_fields_`**: just prepends `_ipc_version` (the original behavior).

    Mixing both styles in one class raises TypeError.
    """

    def __new__(mcs, name, bases, namespace):
        is_buffer_subclass = any(hasattr(b, "_is_buffer_base") for b in bases)
        if not is_buffer_subclass:
            return super().__new__(mcs, name, bases, namespace)

        explicit_fields = "_fields_" in namespace
        annotations = {
            n: a
            for n, a in namespace.get("__annotations__", {}).items()
            if not n.startswith("_")
        }

        if explicit_fields and annotations:
            raise TypeError(
                f"{name}: cannot mix `_fields_` with field annotations. "
                f"Pick one style: either define `_fields_` explicitly, or use "
                f"`field_name: type` annotations (recommended)."
            )

        scalar_defaults: dict[str, Any] = {}
        properties_to_install: dict[str, property] = {}

        if annotations:
            fields: list[tuple[str, type]] = []
            for field_name, annotation in annotations.items():
                if annotation is np.ndarray:
                    if field_name not in namespace:
                        raise TypeError(
                            f"{name}.{field_name}: np.ndarray fields require a "
                            f"class-level default that specifies shape and dtype. "
                            f"Example: `{field_name}: np.ndarray = np.zeros((H, W, 3), dtype=np.uint8)`."
                        )
                    default = namespace[field_name]
                    if not isinstance(default, np.ndarray):
                        raise TypeError(
                            f"{name}.{field_name}: default must be a numpy.ndarray "
                            f"(got {type(default).__name__})."
                        )
                    storage_name = f"_{field_name}_data"
                    fields.append(
                        (storage_name, _make_ctypes_array(default.dtype, default.shape))
                    )
                    properties_to_install[field_name] = _make_array_property(
                        storage_name, default.shape, default.dtype
                    )
                    # Pop the class-level template so it doesn't shadow the property.
                    del namespace[field_name]
                elif annotation in _PYTHON_TO_CTYPES:
                    fields.append((field_name, _PYTHON_TO_CTYPES[annotation]))
                    if field_name in namespace:
                        scalar_defaults[field_name] = namespace.pop(field_name)
                elif _is_ctypes_scalar(annotation):
                    fields.append((field_name, annotation))
                    if field_name in namespace:
                        scalar_defaults[field_name] = namespace.pop(field_name)
                else:
                    raise TypeError(
                        f"{name}.{field_name}: unsupported annotation {annotation!r}. "
                        f"Use float, int, bool, np.ndarray (with default), or a ctypes "
                        f"scalar type (e.g. ctypes.c_int32, ctypes.c_uint8)."
                    )

            namespace["_fields_"] = [("_ipc_version", ctypes.c_uint32)] + fields
        elif explicit_fields:
            user_fields = namespace["_fields_"]
            namespace["_fields_"] = [("_ipc_version", ctypes.c_uint32)] + list(user_fields)

        namespace["_scalar_defaults_"] = scalar_defaults
        # Install array view properties before class creation so they're class attributes.
        for prop_name, prop in properties_to_install.items():
            namespace[prop_name] = prop

        return super().__new__(mcs, name, bases, namespace)


class Buffer(ctypes.Structure, metaclass=_BufferMeta):
    """Base class for IPC buffers.

    Subclasses define their fields either via annotations (recommended) or via the
    explicit `_fields_` form. See the module docstring for examples. The
    `_ipc_version` field is prepended automatically on every subclass.
    """

    _is_buffer_base = True
    _version_: int = 0
    _scalar_defaults_: dict[str, Any] = {}

    def __init__(self, **kwargs):
        merged = {**type(self)._scalar_defaults_, **kwargs}
        super().__init__(**merged)
        self._ipc_version = type(self)._version_

    def to_bytes(self) -> bytes:
        return bytes(self)

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        if len(data) != ctypes.sizeof(cls):
            raise ValueError(
                f"Expected {ctypes.sizeof(cls)} bytes, got {len(data)}"
            )
        return cls.from_buffer_copy(data)

    @classmethod
    def check_version(cls, instance: "Buffer") -> None:
        if instance._ipc_version != cls._version_:
            raise IncompatibleVersionError(
                f"Expected version {cls._version_}, got {instance._ipc_version}"
            )


def buffer_from_arrays(name: str, version: int = 1, **arrays: np.ndarray) -> type[Buffer]:
    """Generate a Buffer subclass from numpy arrays at runtime.

    Use this when the shape is only known at runtime (e.g. a camera resolution
    queried from the device). For static shapes, prefer the annotation DSL:

        class Frame(Buffer):
            _version_ = 1
            bgr: np.ndarray = np.zeros((480, 640, 3), dtype=np.uint8)

    Runtime example::

        FrameBuffer = buffer_from_arrays(
            "FrameBuffer", version=1,
            bgr=np.zeros((h, w, 3), dtype=np.uint8),
        )
        buf = FrameBuffer()
        buf.bgr[:] = frame
    """
    if not arrays:
        raise ValueError("At least one array must be provided")

    fields: list[tuple[str, type]] = []
    array_info: dict[str, tuple[tuple[int, ...], np.dtype]] = {}
    for field_name, arr in arrays.items():
        ct_array = _make_ctypes_array(arr.dtype, arr.shape)
        fields.append((f"_{field_name}_data", ct_array))
        array_info[field_name] = (arr.shape, arr.dtype)

    cls = _BufferMeta(
        name,
        (Buffer,),
        {"_version_": version, "_fields_": fields},
    )

    for field_name, (shape, dtype) in array_info.items():
        setattr(cls, field_name, _make_array_property(f"_{field_name}_data", shape, dtype))

    return cls
