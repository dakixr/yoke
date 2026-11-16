"""Peer-uid authorization for the daemon's Unix socket listener.

Connections on the Unix socket carry no bearer token; the kernel-reported uid
of the connecting process is the credential. TCP connections pass through and
are authorized by the bearer token in ``yoke.http.auth``.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import os
import socket
import struct

LOGGER = logging.getLogger(__name__)


def peer_uid(sock: socket.socket) -> int | None:
    """Return the uid of the process on the other end of a Unix socket."""
    if sock.family != socket.AF_UNIX:
        return None
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        # macOS and the BSDs have no SO_PEERCRED; getpeereid(2) reports the same uid.
        return _getpeereid_uid(sock.fileno())
    raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _getpeereid_uid(fd: int) -> int | None:
    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        return None
    libc = ctypes.CDLL(libc_path, use_errno=True)
    getpeereid = getattr(libc, "getpeereid", None)
    if getpeereid is None:
        return None
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    if getpeereid(fd, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "getpeereid failed")
    return uid.value


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
