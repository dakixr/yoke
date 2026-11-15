"""``SO_PEERCRED`` authorization for the daemon's Unix socket listener.

Connections on the Unix socket carry no bearer token; the kernel-reported uid
of the connecting process is the credential. TCP connections pass through and
are authorized by the bearer token in ``yoke.http.auth``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct

LOGGER = logging.getLogger(__name__)


def peer_uid(sock: socket.socket) -> int | None:
    """Return the uid of the process on the other end of a Unix socket."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None or sock.family != socket.AF_UNIX:
        return None
    raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def peer_authorized(uid: int | None, *, owner_uid: int | None = None) -> bool:
    """Only the daemon's own user may use the socket."""
    expected = os.getuid() if owner_uid is None else owner_uid
    return uid is not None and uid == expected


def connection_authorized(sock: socket.socket | None) -> bool:
    """Admit TCP connections (token-checked later) and same-uid Unix peers."""
    if sock is None or sock.family != socket.AF_UNIX:
        return True
    try:
        return peer_authorized(peer_uid(sock))
    except OSError:
        return False


def peer_checked(protocol_class: type[asyncio.Protocol]) -> type[asyncio.Protocol]:
    """Wrap a uvicorn HTTP protocol so foreign-uid Unix peers are dropped."""

    class PeerCheckedProtocol(  # type: ignore[valid-type,misc]
        protocol_class,  # ty: ignore[unsupported-base]
    ):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            super().connection_made(transport)  # type: ignore[arg-type]
            if not connection_authorized(transport.get_extra_info("socket")):
                LOGGER.warning("Rejected a daemon socket connection from another user")
                transport.close()

    PeerCheckedProtocol.__name__ = f"PeerChecked{protocol_class.__name__}"
    return PeerCheckedProtocol
