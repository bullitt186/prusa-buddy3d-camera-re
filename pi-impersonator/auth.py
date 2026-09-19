"""Stdlib-only Socket.IO authentication gate (GAP-AUTH-01).

Firmware 3.1.6 continues its post-authentication flow only on the successful ACK
path. This module is deliberately free of ``socketio``/``aiohttp`` so the
predicate can be unit-tested on the host.
"""


def auth_ack_is_success(ack):
    """Return True only for the exact integer success ACK value ``0``.

    Recovered 3.1.6: the ``camera_authentication`` ACK callback `FUN_000a05e4`
    calls `FUN_0009e53c`, which logs "Authentication successful" and returns
    success only when the ack integer is ``0`` (values ``1``/``2`` are errors).
    Earlier this predicate required ``1``, which was an artifact of the swapped
    auth field order (see `signaling._authenticate`).

    ``type(ack) is int`` (not ``isinstance``) rejects ``bool``: ``False`` (== 0)
    must not pass. Any other value (``1``, ``5``, ``'0'``, ``None``, malformed
    payloads) is a failed authentication and must not trigger post-auth messages.
    """
    return type(ack) is int and ack == 0
