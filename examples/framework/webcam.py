"""This script reads from the webcam with OpenCV and publishes the images to a topic over shared memory.
Another process, started from this same script, subscribes to the images and shows them in a window.

This script requires that you have OpenCV installed, you can do this with: `pip install opencv-contrib-python`.
"""
import ctypes
from typing import Final

import cv2
import numpy as np
from loguru import logger

from airo_ipc import Buffer, Transport, buffer_from_arrays
from airo_ipc.framework import Node

TOPIC_RESOLUTION: Final[str] = "resolution"
TOPIC_BGR: Final[str] = "frame"


class Resolution(Buffer):
    """We will send the resolution of the webcam over the network."""
    _version_ = 1
    _fields_ = [
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
    ]


def make_frame_buffer(width: int, height: int) -> type[Buffer]:
    """Create a Buffer type for webcam frames with the given resolution."""
    return buffer_from_arrays(
        "WebcamFrame",
        bgr=np.zeros((height, width, 3), dtype=np.uint8),
    )


class WebcamPublisher(Node):
    """The publisher will open the webcam and publish the resolution and frame in a loop."""
    def _setup(self):
        logger.info("Opening webcam.")
        self._camera = cv2.VideoCapture(0)

        logger.info("Getting resolution.")
        width = int(self._camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self._camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self._FrameBuffer = make_frame_buffer(width, height)

        logger.info("Registering publishers.")
        self._register_publisher(TOPIC_RESOLUTION, Resolution, Transport.NETWORK)
        self._register_publisher(TOPIC_BGR, self._FrameBuffer, Transport.LOCAL)

        self._resolution = Resolution(width=width, height=height)

    def _step(self):
        ret, frame = self._camera.read()

        if not ret:
            logger.error("Could not read frame from webcam. Stopping publisher.")
            self.stop()
            return

        self._publish(TOPIC_RESOLUTION, self._resolution)

        msg = self._FrameBuffer()
        msg.bgr[:] = frame
        self._publish(TOPIC_BGR, msg)

    def _teardown(self):
        self._camera.release()


class WebcamSubscriber(Node):
    """The subscriber will show the frames in a window."""
    def _setup(self):
        logger.info("Creating webcam window.")
        cv2.namedWindow("Webcam", cv2.WINDOW_NORMAL)
        logger.info("Subscribing to resolution messages.")
        self._subscribe(TOPIC_RESOLUTION, Resolution, Transport.NETWORK, callback=self._on_receive_resolution)

    def _on_receive_resolution(self, resolution: Resolution):
        if TOPIC_BGR in self._subscribers:
            return

        FrameBuffer = make_frame_buffer(resolution.width, resolution.height)
        logger.info("Received resolution message. Subscribing to BGR messages.")
        self._subscribe(TOPIC_BGR, FrameBuffer, Transport.LOCAL, callback=self._on_receive_frame)

    def _on_receive_frame(self, frame):
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
