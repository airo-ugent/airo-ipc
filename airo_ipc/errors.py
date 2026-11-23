"""Exceptions raised by airo-ipc."""


class AiroIpcError(Exception):
    """Base class for all airo-ipc errors."""


class SchemaMismatchError(AiroIpcError):
    """A received message does not match the local message definition.

    The sender and the receiver use different definitions of the same message class: a field name,
    field type, array shape, field order or the class name differs. Restart both sides with the same
    message definition.
    """


class LoanReleasedError(AiroIpcError):
    """A loaned message was used after its ``with`` block ended."""


class ReadOnlyMessageError(AiroIpcError):
    """A received loan was written to. Received loans are read-only; use ``msg.copy()``."""


class TooManyLoansError(AiroIpcError):
    """A subscriber holds too many received loans at the same time.

    Each live ``receive_loan()`` view keeps one shared-memory sample borrowed. Release views (let
    them go out of scope) or copy the data, or raise ``Iox2Sizing.max_borrowed_per_subscriber``.
    """


class NoServerError(AiroIpcError):
    """A service call found no server for the topic."""


class ServiceError(AiroIpcError):
    """The server's handler raised an exception while handling the request.

    Attributes:
        remote_message: The error text sent by the server (``"<ExceptionType>: <message>"``).
    """

    def __init__(self, remote_message: str) -> None:
        super().__init__(remote_message)
        self.remote_message = remote_message


class ClosedError(AiroIpcError):
    """The endpoint or its context was closed."""
