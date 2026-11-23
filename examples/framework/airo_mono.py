"""An airo-camera-toolkit ``RGBCamera`` whose images come from another process.

A ``Worker`` reads the webcam and publishes RGB frames plus intrinsics over ``Local()``, and the
resolution over ``Network()`` as a latched topic. ``RemoteCamera`` implements the RGBCamera
interface on top of a subscriber, in the main process, without a Worker.

Requires airo-camera-toolkit: https://github.com/airo-ugent/airo-mono
Run: ``uv run python examples/framework/airo_mono.py``; press "q" in the window to quit.
"""

from __future__ import annotations

import logging
from typing import Final

import cv2
import numpy as np
from airo_camera_toolkit.cameras.opencv_videocapture.opencv_videocapture import (
    OpenCVVideoCapture,
)
from airo_camera_toolkit.interfaces import RGBCamera
from airo_camera_toolkit.utils.image_converter import ImageConverter
from airo_typing import (
    CameraIntrinsicsMatrixType,
    CameraResolutionType,
    NumpyFloatImageType,
    NumpyIntImageType,
)
from numpy.typing import NDArray

import airo_ipc as ipc
from airo_ipc.framework import Worker

logger = logging.getLogger("airo_mono")

TOPIC_RESOLUTION: Final[str] = "camera/resolution"
TOPIC_RGB: Final[str] = "camera/rgb"


class Resolution(ipc.Message):
    width: ipc.i32
    height: ipc.i32


class CameraFrame(ipc.Message):
    rgb: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)
    intrinsics: NDArray[np.float64] = ipc.Array((3, 3), np.float64)


class CameraPublisher(Worker):
    def _setup(self) -> None:
        self._camera = OpenCVVideoCapture(intrinsics_matrix=np.eye(3))
        width, height = self._camera.resolution
        resolution = self._register_publisher(
            TOPIC_RESOLUTION, Resolution, ipc.Network(), latched=True
        )
        resolution.publish(Resolution(width=width, height=height))
        frame_type = CameraFrame.with_shapes(rgb=(height, width, 3))
        self._frames = self._register_publisher(TOPIC_RGB, frame_type, ipc.Local())
        self._intrinsics = self._camera.intrinsics_matrix()

    def _step(self) -> None:
        rgb = self._camera.get_rgb_image_as_int()
        with self._frames.loan() as frame:
            frame.rgb[:] = rgb
            frame.intrinsics[:] = self._intrinsics

    def _teardown(self) -> None:
        pass


class RemoteCamera(RGBCamera):
    """An RGBCamera backed by a subscriber. A Worker is not required to use airo-ipc."""

    def __init__(self) -> None:
        super().__init__()
        resolution_sub = ipc.subscriber(TOPIC_RESOLUTION, Resolution, ipc.Network())
        resolution = resolution_sub.receive(
            timeout=30.0
        )  # latched: arrives even if we are late
        if resolution is None:
            raise TimeoutError("no camera resolution received within 30 s")
        resolution_sub.close()
        self._resolution = (resolution.width, resolution.height)
        frame_type = CameraFrame.with_shapes(
            rgb=(resolution.height, resolution.width, 3)
        )
        self._frames = ipc.subscriber(TOPIC_RGB, frame_type, ipc.Local())

    @property
    def resolution(self) -> CameraResolutionType:
        return self._resolution

    def _grab_images(self) -> None:
        frame = self._frames.receive(timeout=1.0)  # an owned copy: safe to keep
        if frame is None:
            raise TimeoutError("no camera frame received within 1 s")
        self._rgb = frame.rgb
        self._intrinsics_matrix = frame.intrinsics

    def _retrieve_rgb_image(self) -> NumpyFloatImageType:
        return ImageConverter.from_numpy_int_format(self._rgb).image_in_numpy_format

    def _retrieve_rgb_image_as_int(self) -> NumpyIntImageType:
        return self._rgb

    def intrinsics_matrix(self) -> CameraIntrinsicsMatrixType:
        return self._intrinsics_matrix


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    publisher = CameraPublisher(update_frequency=20, verbose=True)
    publisher.start()
    camera = RemoteCamera()
    cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)
    try:
        while True:
            rgb = camera.get_rgb_image_as_int()
            cv2.imshow(
                "Webcam",
                ImageConverter.from_numpy_int_format(rgb).image_in_opencv_format,
            )
            if cv2.waitKey(1) == ord("q"):
                break
    finally:
        publisher.stop()
        publisher.join()
        cv2.destroyAllWindows()
