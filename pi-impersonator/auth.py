"""Stdlib-only Socket.IO authentication gate (GAP-AUTH-01).

Firmware 3.1.6 continues its post-authentication flow only on the successful ACK
path. This module is deliberately free of ``socketio``/``aiohttp`` so the
predicate can be unit-tested on the host.
"""


def auth_ack_is_success(ack):
    """Return True only for the exact integer success ACK value ``1``.

    ``type(ack) is int`` (not ``isinstance``) rejects ``bool``: ``True`` must not
    pass. Any other value (``0``, ``5``, ``'1'``, ``None``, malformed payloads) is
    a failed authentication and must not trigger post-auth messages.
    """
    return type(ack) is int and ack == 1
