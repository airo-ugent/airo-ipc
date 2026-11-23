"""Check what a static type checker (mypy) sees for user-defined messages."""

import textwrap

import pytest

mypy_api = pytest.importorskip("mypy.api")

SNIPPET = textwrap.dedent("""
    import numpy as np
    from numpy.typing import NDArray
    from airo_ipc.message import Array, Message, i32

    class CameraFrame(Message):
        stamp: float
        frame_id: i32
        rgb: NDArray[np.uint8] = Array((None, None, 3), np.uint8)

    Frame = CameraFrame.with_shapes(rgb=(480, 640, 3))
    frame = Frame(stamp=1.0, frame_id=2)
    reveal_type(frame.rgb)
    reveal_type(frame.frame_id)
    reveal_type(frame.stamp)
    reveal_type(Frame)
    reveal_type(CameraFrame.from_bytes(b""))
    reveal_type(frame.copy())
    """)


def test_mypy_sees_field_types(tmp_path: object) -> None:
    import pathlib

    path = pathlib.Path(str(tmp_path)) / "snippet.py"
    path.write_text(SNIPPET)
    stdout, stderr, status = mypy_api.run(
        [str(path), "--strict", "--no-incremental", "--follow-imports=silent"]
    )
    reveals = [
        line.split("note: Revealed type is ")[1]
        for line in stdout.splitlines()
        if "Revealed type" in line
    ]
    # The ndarray shape parameter differs between numpy versions; check only the dtype part.
    assert (
        reveals[0].startswith('"numpy.ndarray[') and "unsignedinteger" in reveals[0]
    ), stdout
    assert "_8Bit" in reveals[0], stdout
    assert reveals[1:] == [
        '"builtins.int"',
        '"builtins.float"',
        '"type[snippet.CameraFrame]"',
        '"snippet.CameraFrame"',
        '"snippet.CameraFrame"',
    ], stdout
    errors = [line for line in stdout.splitlines() if ": error:" in line]
    assert errors == [], stdout
