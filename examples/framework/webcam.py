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

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl
from airo_ipc.framework.framework import IpcKind
from airo_ipc.framework.node import Node

TOPIC_RESOLUTION: Final[str] = "resolution"
TOPIC_BGR: Final[str] = "frame"


@dataclass
class ResolutionIdl(IdlStruct):
    """We will send the resolution of the webcam over DDS: we need to define an IDL struct for this."""
    width: int
    height: int


@dataclass
class WebcamFrame(BaseIdl):
    """We will send the BGR frames over shared memory: we need to derive from BaseIDL."""
    bgr: np.ndarray

    @staticmethod
    def with_resolution(width: int, height: int):
        """We may not know the resolution of the webcam when we create the frame, so we need a factory method."""
        return WebcamFrame(bgr=np.zeros((height, width, 3), dtype=np.uint8))


class WebcamPublisher(Node):
    """The publisher will open the webcam and publish the resolution and frame in a loop."""
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
        """The _step method is called in a loop by the Node superclass."""
        ret, frame = self._camera.read()

        if not ret:
            logger.error("Could not read frame from webcam. Stopping publisher.")
            self.stop()

        self._publish(TOPIC_RESOLUTION, ResolutionIdl(width=frame.shape[1], height=frame.shape[0]))
        self._publish(TOPIC_BGR, WebcamFrame(bgr=frame))

    def _teardown(self):
        """This is where we can clean up resources when the publisher is stopped."""
        self._camera.release()


class WebcamSubscriber(Node):
    """The subscriber will show the frames in a window."""
    def _setup(self):
        logger.info("Creating webcam window.")
        cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)
        logger.info("Subscribing to resolution messages.")
        self._subscribe(TOPIC_RESOLUTION, ResolutionIdl, IpcKind.DDS, self._on_receive_resolution)
        # We cannot yet subscribe to the BGR topic, because we do not yet know the resolution. We will do this in the
        # resolution callback.

    def _on_receive_resolution(self, resolution: ResolutionIdl):
        # If we already subscribed to the BGR topic, we do not need to do it again.
        if TOPIC_BGR in self._readers:
            return

        # It is safe to subscribe to topics in callbacks, because a copy of the existing subscriptions is made in the
        # Node superclass.
        logger.info("Received resolution message. Subscribing to BGR messages.")
        self._subscribe(TOPIC_BGR, WebcamFrame.with_resolution(resolution.width, resolution.height),
                        IpcKind.SHARED_MEMORY, self._on_receive_frame)

    def _on_receive_frame(self, frame: WebcamFrame):
        # Here, we show the frame in a window in the callback function. This is not ideal, because the callback function
        # should be as fast as possible. In a real application, you could use a queue to pass the frames to another
        # thread that shows the frames in a window.
        cv2.imshow("Webcam", frame.bgr)
        key = cv2.waitKey(1)
        if key == ord("q"):
            logger.info("Closing webcam window.")
            self.stop()

    def _step(self):
        pass

    def _teardown(self):
        # We need to close the window when the subscriber is stopped.
        cv2.destroyAllWindows()


if __name__ == '__main__':
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
