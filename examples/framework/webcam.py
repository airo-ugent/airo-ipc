"""This script reads from the webcam with OpenCV and publishes the images to a topic over shared memory.
Another process, started from this same script, subscribes to the images and shows them in a window.

This script requires that you have OpenCV installed, you can do this with: `pip install opencv-contrib-python`.
"""
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np
from cyclonedds.idl import IdlStruct
from loguru import logger

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIDL
from airo_ipc.framework.framework import initialize_ipc, IpcKind
from airo_ipc.framework.node import Node

TOPIC_RESOLUTION: Final[str] = "resolution"
TOPIC_BGR: Final[str] = "frame"


@dataclass
class ResolutionIdl(IdlStruct):
    width: int
    height: int


@dataclass
class WebcamFrame(BaseIDL):
    bgr: np.ndarray

    @staticmethod
    def with_resolution(width: int, height: int):
        return WebcamFrame(bgr=np.zeros((height, width, 3), dtype=np.uint8))


class WebcamPublisher(Node):
    def _setup(self):
        logger.info("Opening webcam.")
        self._camera = cv2.VideoCapture(0)

        logger.info("Getting resolution.")
        width, height = int(self._camera.get(cv2.CAP_PROP_FRAME_WIDTH)), int(
            self._camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

        logger.info("Registering publishers.")
        self._register_publisher(TOPIC_RESOLUTION, ResolutionIdl, IpcKind.DDS)
        self._register_publisher(TOPIC_BGR, WebcamFrame.with_resolution(width, height), IpcKind.SHARED_MEMORY)

    def _step(self):
        ret, frame = self._camera.read()

        if not ret:
            logger.error("Could not read frame from webcam. Stopping publisher.")
            self.stop()

        self._publish(TOPIC_RESOLUTION, ResolutionIdl(width=frame.shape[1], height=frame.shape[0]))
        self._publish(TOPIC_BGR, WebcamFrame(bgr=frame))

    def _teardown(self):
        self._camera.release()


class WebcamSubscriber(Node):
    def _setup(self):
        logger.info("Creating webcam window.")
        cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)
        logger.info("Subscribing to resolution messages.")
        self._subscribe(TOPIC_RESOLUTION, ResolutionIdl, IpcKind.DDS, self._on_receive_resolution)

    def _on_receive_resolution(self, resolution: ResolutionIdl):
        if TOPIC_BGR in self._readers:
            return

        logger.info("Received resolution message. Subscribing to RGB messages.")
        self._subscribe(TOPIC_BGR, WebcamFrame.with_resolution(resolution.width, resolution.height),
                        IpcKind.SHARED_MEMORY, self._on_receive_frame)

    def _on_receive_frame(self, frame: WebcamFrame):
        cv2.imshow("Webcam", frame.bgr)
        key = cv2.waitKey(1)
        if key == ord("q"):
            logger.info("Closing webcam window.")
            self.stop()

    def _step(self):
        pass

    def _teardown(self):
        cv2.destroyAllWindows()


if __name__ == '__main__':
    initialize_ipc()

    logger.info("Creating publisher.")
    publisher = WebcamPublisher(20, True)
    logger.info("Starting publisher.")
    publisher.start()

    logger.info("Creating subscriber.")
    subscriber = WebcamSubscriber(20, True)
    logger.info("Starting subscriber.")
    subscriber.start()

    logger.info("Joining subscriber: will quit when the user pressed 'q' with the CV2 window in focus.")
    subscriber.join()
    publisher.stop()
