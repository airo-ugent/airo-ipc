"""Message tests with evaluated annotations (no `from __future__ import annotations`)."""

import numpy as np
from numpy.typing import NDArray

from airo_ipc.message import Array, Message, u8


class Eager(Message):
    count: u8 = 3
    values: NDArray[np.float64] = Array((3,), np.float64)


def test_eager_annotations() -> None:
    e = Eager(values=[1.0, 2.0, 3.0])
    assert e.count == 3
    assert e.values.tolist() == [1.0, 2.0, 3.0]


def test_eager_local_class() -> None:
    class Local(Message):
        x: float

    assert Local(x=2.0).x == 2.0
