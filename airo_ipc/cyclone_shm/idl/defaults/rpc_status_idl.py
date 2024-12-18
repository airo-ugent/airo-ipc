from dataclasses import dataclass, field

from cyclonedds.idl import IdlStruct

from airo_ipc.cyclone_shm.idl.defaults.rpc_idl import RPCIdl


class RPCStatus(RPCIdl):
    @dataclass
    class Request(IdlStruct, typename="RPCStatusRequest.Msg"):
        timestamp: float = field(metadata={"id": 0})

    @dataclass
    class Response(IdlStruct, typename="RPCStatusResponse.Msg"):
        timestamp: float = field(metadata={"id": 0})
