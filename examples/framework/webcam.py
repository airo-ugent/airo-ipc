"""Read webcam frames with OpenCV in one process and show them in another.

- Frames go over ``Local()`` (iceoryx2 zero-copy shared memory), written in place with
  ``loan()``.
- The resolution goes over ``Network()`` (Zenoh) as a *latched* topic: a subscriber that starts
  later still receives it. The subscriber needs it to build the frame type, because the frame
  shape is only known at runtime.

Requires OpenCV: ``uv add opencv-contrib-python`` (or ``pip install opencv-contrib-python``).
Run: ``uv run python examples/framework/webcam.py``; press "q" in the window to quit.
"""

from __future__ import annotations

import logging
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

import airo_ipc as ipc
from airo_ipc.framework import Worker

logger = logging.getLogger("webcam")

TOPIC_RESOLUTION: Final[str] = "webcam/resolution"
TOPIC_BGR: Final[str] = "webcam/bgr"


class Resolution(ipc.Message):
    width: ipc.i32
    height: ipc.i32


class WebcamFrame(ipc.Message):
    """The height and width are only known at runtime: see WebcamFrame.with_shapes(...)."""

    bgr: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)


class WebcamPublisher(Worker):
    def _setup(self) -> None:
        logger.info("Opening the webcam.")
        self._camera = cv2.VideoCapture(0)
        width = int(self._camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self._camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_type = WebcamFrame.with_shapes(bgr=(height, width, 3))

        resolution = self._register_publisher(
            TOPIC_RESOLUTION, Resolution, ipc.Network(), latched=True
        )
        resolution.publish(Resolution(width=width, height=height))
        self._frames = self._register_publisher(TOPIC_BGR, frame_type, ipc.Local())

    def _step(self) -> None:
        with self._frames.loan() as frame:
            ok = self._camera.read(frame.bgr)[
                0
            ]  # OpenCV writes straight into shared memory
            if not ok:
                raise RuntimeError("could not read a frame from the webcam")

    def _teardown(self) -> None:
        self._camera.release()


class WebcamViewer(Worker):
    def _setup(self) -> None:
        cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)
        self._subscribe(
            TOPIC_RESOLUTION, Resolution, ipc.Network(), callback=self._on_resolution
        )

    def _on_resolution(self, resolution: Resolution) -> None:
        if TOPIC_BGR in self._subscribers:
            return
        logger.info(
            "Resolution %dx%d: subscribing to frames.",
            resolution.width,
            resolution.height,
        )
        frame_type = WebcamFrame.with_shapes(
            bgr=(resolution.height, resolution.width, 3)
        )
        self._subscribe(TOPIC_BGR, frame_type, ipc.Local(), callback=self._on_frame)

    def _on_frame(self, frame: WebcamFrame) -> None:
        cv2.imshow("Webcam", frame.bgr)
        if cv2.waitKey(1) == ord("q"):
            self.stop()

    def _step(self) -> None:
        pass

    def _teardown(self) -> None:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    publisher = WebcamPublisher(update_frequency=30, verbose=True)
    viewer = WebcamViewer(update_frequency=60)
    publisher.start()
    viewer.start()
    viewer.join()  # returns when "q" is pressed in the window
    publisher.stop()
    publisher.join()
