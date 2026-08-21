"""Bearer-token protection for the MCP HTTP endpoint."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

if TYPE_CHECKING:
    from yoke.mcp_server.auth import SingleUserOAuthProvider


class BearerTokenMiddleware:
    """Require a configured static or OAuth bearer token for the MCP endpoint."""

    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        *,
        oauth_provider: SingleUserOAuthProvider | None = None,
        resource_metadata_url: str | None = None,
    ) -> None:
        self.app = app
        self.token = token
        self.oauth_provider = oauth_provider
        self.resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return
        if not self.token and self.oauth_provider is None:
            await self.app(scope, receive, send)
            return

        authorization = _header(scope, b"authorization")
        candidate = None
        if authorization and authorization.lower().startswith(b"bearer "):
            candidate = authorization[7:].decode(errors="ignore")
        valid = bool(
            candidate
            and self.token
            and hmac.compare_digest(candidate.encode(), self.token.encode())
        )
        if not valid and candidate and self.oauth_provider is not None:
            valid = await self.oauth_provider.load_access_token(candidate) is not None
        if not valid:
            challenge = (
                'Bearer error="invalid_token"'
                if self.oauth_provider is not None
                else "Bearer"
            )
            if self.resource_metadata_url:
                challenge += f', resource_metadata="{self.resource_metadata_url}"'
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _header(scope: Scope, name: bytes) -> bytes | None:
    return next((value for key, value in scope.get("headers", []) if key == name), None)
