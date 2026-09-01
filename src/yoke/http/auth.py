"""Bearer authentication for the privileged local daemon."""

from __future__ import annotations

import secrets

from fastapi import HTTPException
from fastapi import Request
from fastapi import status


def require_auth(request: Request) -> None:
    """Require the configured daemon bearer token when authentication is enabled."""
    expected = request.app.state.auth_token
    if expected is None:
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
