"""This script implements the airo-camera-toolkit RGBCamera interface for a webcam using OpenCV.

This script reads from the webcam with OpenCV and publishes the images to a topic over shared memory.
Another process, started from this same script, subscribes to the images and shows them in a window.

This script requires you to install the airo-camera-toolkit, which you can do by following the instructions here:
https://github.com/airo-ugent/airo-mono
"""
import time
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np
from airo_camera_toolkit.cameras.opencv_videocapture.opencv_videocapture import OpenCVVideoCapture
from airo_camera_toolkit.interfaces import RGBCamera
from airo_camera_toolkit.utils.image_converter import ImageConverter
from airo_typing import CameraIntrinsicsMatrixType, NumpyIntImageType, NumpyFloatImageType, CameraResolutionType
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from loguru import logger

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl
from airo_ipc.cyclone_shm.patterns.ddsreader import DDSReader
from airo_ipc.cyclone_shm.patterns.sm_reader import SMReader
from airo_ipc.framework.framework import IpcKind
from airo_ipc.framework.node import Node

TOPIC_RESOLUTION: Final[str] = "resolution"
TOPIC_RGB: Final[str] = "frame"


@dataclass
class ResolutionIdl(IdlStruct):
    """We will send the resolution of the webcam over DDS: we need to define an IDL struct for this."""
    width: int
    height: int


@dataclass
class WebcamFrame(BaseIdl):
    """We will send the RGB frames over shared memory: we need to derive from BaseIDL."""
    rgb: np.ndarray
    intrinsics: np.ndarray

    @staticmethod
    def with_resolution(width: int, height: int):
        """We may not know the resolution of the webcam when we create the frame, so we need a factory method."""
        return WebcamFrame(rgb=np.zeros((height, width, 3), dtype=np.uint8), intrinsics=np.zeros((3, 3)))


class RGBCameraPublisher(Node):
    """The publisher will open the webcam and publish the resolution and frame in a loop."""

    def _setup(self):
        logger.info("Opening camera.")
        self._camera = OpenCVVideoCapture(intrinsics_matrix=np.eye(3))

        logger.info("Getting resolution.")
        width, height = self._camera.resolution

        logger.info("Registering publishers.")
        self._register_publisher(TOPIC_RESOLUTION, ResolutionIdl, IpcKind.DDS)
        self._register_publisher(TOPIC_RGB, WebcamFrame.with_resolution(width, height), IpcKind.SHARED_MEMORY)

    def _step(self):
        """The _step method is called in a loop by the Node superclass."""
        rgb = self._camera.get_rgb_image_as_int()

        self._publish(TOPIC_RESOLUTION,
                      ResolutionIdl(width=self._camera.resolution[0], height=self._camera.resolution[1]))
        self._publish(TOPIC_RGB, WebcamFrame(rgb=rgb, intrinsics=self._camera.intrinsics_matrix()))

    def _teardown(self):
        pass


class WebcamSubscriber(RGBCamera):
    """Remark how we don't *need* to inherit from the Node superclass; this only depends on your use cases.
        Here, we can simplify our code by not using callbacks, but running the subscriber in the main process."""

    def __init__(self):
        super().__init__()

        self._cyclone_dp = DomainParticipant()
        self._reader_resolution = DDSReader(self._cyclone_dp, TOPIC_RESOLUTION, ResolutionIdl)
        # Wait for the first resolution message.
        resolution = None
        while resolution is None:
            resolution = self._reader_resolution()
            logger.info("Did not yet receive resolution message. Sleeping for 100 milliseconds...")
            time.sleep(0.1)
        self._reader_rgb = SMReader(self._cyclone_dp, TOPIC_RGB,
                                    WebcamFrame.with_resolution(resolution.width, resolution.height))

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
        frame = self._reader_rgb()
        if frame is not None:
            self._rgb = frame.rgb.copy() # SMReader does not copy arrays: we need to do this ourselves.
            self._intrinsics_matrix = frame.intrinsics


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
