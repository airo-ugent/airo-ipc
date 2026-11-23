"""Exceptions raised by airo-ipc."""


class AiroIpcError(Exception):
    """Base class for all airo-ipc errors."""


class SchemaMismatchError(AiroIpcError):
    """A received message does not match the local message definition.

    The sender and the receiver use different definitions of the same message class: a field name,
    field type, array shape, field order or the class name differs. Restart both sides with the same
    message definition.
    """
