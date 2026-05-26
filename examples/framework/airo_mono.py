"""This script implements the airo-camera-toolkit RGBCamera interface for a webcam using OpenCV.

This script reads from the webcam with OpenCV and publishes the images over iceoryx2 (local
zero-copy SHM) and the resolution over Zenoh (network). Another process, started from this
same script, subscribes to the images and shows them in a window.

This script requires you to install the airo-camera-toolkit, which you can do by following the
instructions here: https://github.com/airo-ugent/airo-mono
"""
import ctypes
import time
from typing import Final

import cv2
import numpy as np
from airo_camera_toolkit.cameras.opencv_videocapture.opencv_videocapture import OpenCVVideoCapture
from airo_camera_toolkit.interfaces import RGBCamera
from airo_camera_toolkit.utils.image_converter import ImageConverter
from airo_typing import CameraIntrinsicsMatrixType, NumpyIntImageType, NumpyFloatImageType, CameraResolutionType
from loguru import logger

from airo_ipc import Buffer, Transport, buffer_from_arrays, create_subscriber
from airo_ipc.framework import Worker

TOPIC_RESOLUTION: Final[str] = "resolution"
TOPIC_RGB: Final[str] = "frame"


class Resolution(Buffer):
    """Resolution of the webcam, sent over the network. Annotation DSL form."""
    _version_ = 1
    width: ctypes.c_int32
    height: ctypes.c_int32


def make_frame_buffer(width: int, height: int) -> type[Buffer]:
    """Buffer type for an RGB frame plus 3x3 intrinsics, sized to the given resolution."""
    return buffer_from_arrays(
        "WebcamFrame",
        rgb=np.zeros((height, width, 3), dtype=np.uint8),
        intrinsics=np.zeros((3, 3), dtype=np.float64),
    )


class RGBCameraPublisher(Worker):
    """The publisher opens the webcam and publishes the resolution and frame in a loop."""

    def _setup(self):
        logger.info("Opening camera.")
        self._camera = OpenCVVideoCapture(intrinsics_matrix=np.eye(3))

        logger.info("Getting resolution.")
        width, height = self._camera.resolution

        self._FrameBuffer = make_frame_buffer(width, height)

        logger.info("Registering publishers.")
        self._register_publisher(TOPIC_RESOLUTION, Resolution, Transport.NETWORK)
        self._register_publisher(TOPIC_RGB, self._FrameBuffer, Transport.LOCAL)

        self._resolution = Resolution(width=width, height=height)
        self._intrinsics = self._camera.intrinsics_matrix()

    def _step(self):
        rgb = self._camera.get_rgb_image_as_int()

        self._publish(TOPIC_RESOLUTION, self._resolution)

        msg = self._FrameBuffer()
        msg.rgb[:] = rgb
        msg.intrinsics[:] = self._intrinsics
        self._publish(TOPIC_RGB, msg)

    def _teardown(self):
        pass


class WebcamSubscriber(RGBCamera):
    """We don't *need* to inherit from Worker; this depends on your use case.
    Here, we use create_subscriber() directly so we can run in the main process without callbacks."""

    def __init__(self):
        super().__init__()

        # Wait for the first resolution message before we can size the frame buffer.
        resolution_sub = create_subscriber(TOPIC_RESOLUTION, Resolution, Transport.NETWORK)
        resolution = None
        while resolution is None:
            resolution = resolution_sub.receive()
            if resolution is None:
                logger.info("Did not yet receive resolution message. Sleeping for 100 ms...")
                time.sleep(0.1)
        self._resolution = (resolution.width, resolution.height)

        FrameBuffer = make_frame_buffer(resolution.width, resolution.height)
        self._frame_sub = create_subscriber(TOPIC_RGB, FrameBuffer, Transport.LOCAL)

    @property
    def resolution(self) -> CameraResolutionType:
        return self._resolution

    def _retrieve_rgb_image(self) -> NumpyFloatImageType:
        return ImageConverter.from_numpy_int_format(self._rgb).image_in_numpy_format

    def _retrieve_rgb_image_as_int(self) -> NumpyIntImageType:
        return self._rgb

    def intrinsics_matrix(self) -> CameraIntrinsicsMatrixType:
        return self._intrinsics_matrix

    def _grab_images(self) -> None:
        frame = self._frame_sub.receive()
        if frame is not None:
            # The default receive() copies, so we own this data — no further .copy() needed.
            self._rgb = np.array(frame.rgb, copy=True)
            self._intrinsics_matrix = np.array(frame.intrinsics, copy=True)


if __name__ == '__main__':
    logger.info("Creating publisher.")
    publisher = RGBCameraPublisher(20, True)
    logger.info("Starting publisher.")
    publisher.start()

    logger.info("Creating subscriber.")
    subscriber = WebcamSubscriber()

    cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)

    while True:
        rgb = subscriber.get_rgb_image_as_int()
        rgb_cv = ImageConverter.from_numpy_int_format(rgb).image_in_opencv_format

        cv2.imshow("Webcam", rgb_cv)
        key = cv2.waitKey(1)
        if key == ord("q"):
            logger.info("Stopping...")
            break

    publisher.stop()
    cv2.destroyAllWindows()
