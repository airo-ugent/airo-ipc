from dataclasses import field, dataclass
from cyclonedds.idl import IdlStruct
from cyclone.cyclone_namespace import CYCLONE_NAMESPACE

@dataclass
class GripperWidthSample(IdlStruct, typename=f"{CYCLONE_NAMESPACE.UR5E_GRIPPER_WIDTH}.Msg"):
    timestamp: float = field(metadata={"id": 0})

    width: float = field(metadata={"id": 1})