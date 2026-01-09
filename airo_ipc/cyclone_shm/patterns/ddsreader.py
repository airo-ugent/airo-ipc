from typing import Any
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlMeta
from cyclonedds.sub import DataReader
from cyclonedds.topic import Topic

from airo_ipc.cyclone_shm.defaults import CYCLONE_DEFAULTS


class DDSReader:
    def __init__(
        self,
        domain_participant: DomainParticipant,
        topic_name: str,
        idl_dataclass: IdlMeta,
    ):
        self.topic: Topic = Topic(domain_participant, topic_name, idl_dataclass)
        self.reader = DataReader(domain_participant, self.topic, CYCLONE_DEFAULTS.QOS)

    def __call__(self) -> Any:
        """Get the latest data from the reader.

        Returns:
            An instance of the idl_dataclass containing the data, or `None`.
        """
        data = self.reader.read()
        if len(data) == 0:
            return None
        return data[-1]

    def take(self) -> Any:
        """Take the latest data from the reader.

        Only returns samples that have not yet been returned in a take operation.
        (https://cyclonedds.io/docs/cyclonedds/latest/about_dds/datareaders.html)

        Returns:
            An instance of the idl_dataclass containing the data, or `None`."""
        data = self.reader.take()
        if len(data) == 0:
            return None
        return data[-1]
