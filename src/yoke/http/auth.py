"""Bearer authentication for the privileged local daemon."""

from __future__ import annotations

import secrets

from fastapi import HTTPException
from fastapi import Request
from fastapi import status


def require_auth(request: Request) -> None:
    """Require the bearer token, except on the daemon's peer-checked Unix socket."""
    expected = request.app.state.auth_token
    if expected is None or via_daemon_socket(request):
        return
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if (
        scheme.casefold() != "bearer"
        or not token
        or not secrets.compare_digest(token, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def via_daemon_socket(request: Request) -> bool:
    """Whether the request arrived on the daemon socket.

    Uvicorn reports a Unix listener as ``(path, None)``; a TCP listener always
    has a port. The listener itself drops connections from other uids
    (``yoke.daemon.peer``), so arrival here is the credential.
    """
    socket_path = getattr(request.app.state, "daemon_socket", None)
    server = request.scope.get("server")
    return (
        socket_path is not None
        and isinstance(server, (tuple, list))
        and len(server) == 2
        and server[1] is None
        and server[0] == socket_path
    )
