from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlMeta, IdlStruct
from cyclonedds.pub import DataWriter
from cyclonedds.topic import Topic

from airo_ipc.cyclone_shm.defaults import CYCLONE_DEFAULTS


class DDSWriter:
    def __init__(
        self,
        domain_participant: DomainParticipant,
        topic_name: str,
        idl_dataclass: IdlMeta,
    ):
        self.topic = Topic(domain_participant, topic_name, idl_dataclass)
        self.writer = DataWriter(domain_participant, self.topic, CYCLONE_DEFAULTS.QOS)

    def __call__(self, msg: IdlStruct):
        self.writer.write(msg)
