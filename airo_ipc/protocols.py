"""Abstract interfaces for IPC patterns."""

from __future__ import annotations

from typing import Callable, Generic, Protocol, TypeVar

from airo_ipc.buffer import Buffer

B = TypeVar("B", bound=Buffer)
Req = TypeVar("Req", bound=Buffer)
Rep = TypeVar("Rep", bound=Buffer)


class Publisher(Protocol[B]):
    def publish(self, msg: B) -> None: ...


class Subscriber(Protocol[B]):
    def receive(self) -> B | None: ...


class Requester(Protocol[Req, Rep]):
    def request(self, msg: Req) -> Rep: ...


class Responder(Protocol[Req, Rep]):
    def listen(self, handler: Callable[[Req], Rep]) -> None: ...
    def stop(self) -> None: ...


class StateWriter(Protocol[B]):
    def set(self, state: B) -> None: ...


class StateReader(Protocol[B]):
    def get(self) -> B | None: ...
