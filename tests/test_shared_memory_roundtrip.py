from dataclasses import dataclass
import threading
import time
from typing import cast
from uuid import uuid4

import numpy as np
from cyclonedds.domain import DomainParticipant

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl
from airo_ipc.cyclone_shm.patterns.sm_reader import SMReader
from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter


@dataclass
class Array2DIdl(BaseIdl):
    values: np.ndarray


def test_shared_memory_roundtrip_2d_array_content() -> None:
    topic_name = f"test_2d_array_{uuid4().hex}"
    writer_participant = DomainParticipant()
    reader_participant = DomainParticipant()
    template = Array2DIdl(values=np.zeros((8, 6), dtype=np.float32))
    writer = SMWriter(writer_participant, topic_name, template, nr_of_buffers=2)

    try:
        expected = np.arange(48, dtype=np.float32).reshape(8, 6)

        def publish_sample() -> None:
            time.sleep(0.1)
            writer(Array2DIdl(values=expected))

        publisher_thread = threading.Thread(target=publish_sample, daemon=True)
        publisher_thread.start()

        reader = SMReader(reader_participant, topic_name, template, nr_of_buffers=2)
        try:
            publisher_thread.join(timeout=2.0)
            assert not publisher_thread.is_alive()
            received = cast(Array2DIdl, reader())
            assert np.array_equal(received.values, expected)
        finally:
            reader.stop()
    finally:
        writer.stop()
        for participant in (writer_participant, reader_participant):
            close = getattr(participant, "close", None)
            if callable(close):
                close()
