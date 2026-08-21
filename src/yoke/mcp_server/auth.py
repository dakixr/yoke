"""Minimal bearer protection for private deployment testing."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send


class BearerTokenMiddleware:
    """Require a static bearer token for the MCP endpoint only."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.token or scope["type"] != "http" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return
        authorization = _header(scope, b"authorization")
        expected = f"Bearer {self.token}".encode()
        if authorization is None or not hmac.compare_digest(authorization, expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _header(scope: Scope, name: bytes) -> bytes | None:
    return next((value for key, value in scope.get("headers", []) if key == name), None)


ASGISend = Callable[[Message], Awaitable[None]]
