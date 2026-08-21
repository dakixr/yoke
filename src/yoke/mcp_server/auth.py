"""Authentication helpers for private and ChatGPT MCP deployments."""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import os
import secrets
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.provider import AuthorizationCode
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.provider import RefreshToken
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from mcp.shared.auth import OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send


_ACCESS_TOKEN_SECONDS = 60 * 60
_REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
_AUTHORIZATION_CODE_SECONDS = 5 * 60
_PENDING_AUTHORIZATION_SECONDS = 10 * 60


@dataclass(slots=True)
class _PendingAuthorization:
    client_id: str
    params: AuthorizationParams
    expires_at: float


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Small persistent OAuth 2.1 provider for one trusted server operator."""

    def __init__(
        self,
        *,
        issuer_url: str,
        resource_url: str,
        authorization_password: str,
        state_file: Path,
        allowed_redirect_hosts: tuple[str, ...] = ("chatgpt.com",),
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url
        self.authorization_password = authorization_password
        self.state_file = state_file.expanduser().resolve()
        self.allowed_redirect_hosts = frozenset(allowed_redirect_hosts)
        self._lock = asyncio.Lock()
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._authorization_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._pending: dict[str, _PendingAuthorization] = {}
        self._load_state()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is not None and client.scope is None:
            return client.model_copy(update={"scope": "yoke"})
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect URI is required",
            )
        for redirect_uri in redirect_uris:
            parsed = urlparse(str(redirect_uri))
            is_local_test = parsed.hostname == "localhost" and parsed.scheme == "http"
            is_allowed_https = (
                parsed.scheme == "https"
                and parsed.hostname in self.allowed_redirect_hosts
            )
            if not (is_local_test or is_allowed_https):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="Redirect URI host is not allowed",
                )
        async with self._lock:
            self._clients[client_info.client_id] = client_info
            self._save_state()

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if params.resource not in {None, self.resource_url}:
            from mcp.server.auth.provider import AuthorizeError

            raise AuthorizeError(
                error="invalid_target",
                error_description="Unknown MCP resource",
            )
        request_id = secrets.token_urlsafe(32)
        async with self._lock:
            self._prune_ephemeral()
            self._pending[request_id] = _PendingAuthorization(
                client_id=client.client_id,
                params=params,
                expires_at=time.time() + _PENDING_AUTHORIZATION_SECONDS,
            )
        return f"{self.issuer_url}/oauth/consent?request_id={request_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._authorization_codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self._lock:
            stored = self._authorization_codes.pop(authorization_code.code, None)
            if stored is None or stored.client_id != client.client_id:
                from mcp.server.auth.provider import TokenError

                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code is invalid or already used",
                )
            token = self._issue_tokens(client.client_id, stored.scopes)
            self._save_state()
            return token

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._lock:
            stored = self._refresh_tokens.pop(refresh_token.token, None)
            if stored is None or stored.client_id != client.client_id:
                from mcp.server.auth.provider import TokenError

                raise TokenError(
                    error="invalid_grant",
                    error_description="Refresh token is invalid or already used",
                )
            token = self._issue_tokens(client.client_id, scopes)
            self._save_state()
            return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self._access_tokens.get(token)
        if access_token and access_token.expires_at:
            if access_token.expires_at < int(time.time()):
                async with self._lock:
                    self._access_tokens.pop(token, None)
                    self._save_state()
                return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._lock:
            self._access_tokens.pop(token.token, None)
            self._refresh_tokens.pop(token.token, None)
            self._save_state()

    async def consent_page(self, request: Request) -> HTMLResponse:
        request_id = request.query_params.get("request_id", "")
        pending = self._pending.get(request_id)
        if pending is None or pending.expires_at < time.time():
            return HTMLResponse("Authorization request expired", status_code=400)
        client = self._clients.get(pending.client_id)
        client_name = (
            html.escape(client.client_name or "ChatGPT") if client else "ChatGPT"
        )
        scopes = html.escape(" ".join(pending.params.scopes or [])) or "Yoke tools"
        escaped_request_id = html.escape(request_id, quote=True)
        return HTMLResponse(
            _consent_html(client_name, scopes, escaped_request_id),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    async def complete_consent(
        self, request: Request
    ) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        password = str(form.get("password", ""))
        pending = self._pending.get(request_id)
        if pending is None or pending.expires_at < time.time():
            return HTMLResponse("Authorization request expired", status_code=400)
        if not hmac.compare_digest(
            password.encode(), self.authorization_password.encode()
        ):
            return HTMLResponse(
                _consent_html(
                    "ChatGPT",
                    "Yoke tools",
                    html.escape(request_id, quote=True),
                    error=True,
                ),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

        async with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None or pending.expires_at < time.time():
                return HTMLResponse("Authorization request expired", status_code=400)
            code_value = secrets.token_urlsafe(32)
            self._authorization_codes[code_value] = AuthorizationCode(
                code=code_value,
                scopes=pending.params.scopes or [],
                expires_at=time.time() + _AUTHORIZATION_CODE_SECONDS,
                client_id=pending.client_id,
                code_challenge=pending.params.code_challenge,
                redirect_uri=pending.params.redirect_uri,
                redirect_uri_provided_explicitly=(
                    pending.params.redirect_uri_provided_explicitly
                ),
                resource=pending.params.resource,
                subject="dakixr",
            )
        query = f"code={code_value}"
        if pending.params.state is not None:
            from urllib.parse import quote

            query += f"&state={quote(pending.params.state, safe='')}"
        separator = "&" if "?" in str(pending.params.redirect_uri) else "?"
        return RedirectResponse(
            f"{pending.params.redirect_uri}{separator}{query}",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access_value = secrets.token_urlsafe(48)
        refresh_value = secrets.token_urlsafe(48)
        self._access_tokens[access_value] = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _ACCESS_TOKEN_SECONDS,
            resource=self.resource_url,
            subject="dakixr",
        )
        self._refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _REFRESH_TOKEN_SECONDS,
            subject="dakixr",
        )
        return OAuthToken(
            access_token=access_value,
            expires_in=_ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    def _prune_ephemeral(self) -> None:
        now = time.time()
        self._pending = {
            key: value
            for key, value in self._pending.items()
            if value.expires_at >= now
        }
        self._authorization_codes = {
            key: value
            for key, value in self._authorization_codes.items()
            if value.expires_at >= now
        }

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text())
        self._clients = {
            key: OAuthClientInformationFull.model_validate(value)
            for key, value in data.get("clients", {}).items()
        }
        self._access_tokens = {
            key: AccessToken.model_validate(value)
            for key, value in data.get("access_tokens", {}).items()
        }
        self._refresh_tokens = {
            key: RefreshToken.model_validate(value)
            for key, value in data.get("refresh_tokens", {}).items()
        }

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "clients": {
                key: value.model_dump(mode="json")
                for key, value in self._clients.items()
            },
            "access_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self._access_tokens.items()
            },
            "refresh_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self._refresh_tokens.items()
            },
        }
        temporary = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")))
        temporary.chmod(0o600)
        os.replace(temporary, self.state_file)


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


def _consent_html(
    client_name: str,
    scopes: str,
    request_id: str,
    *,
    error: bool = False,
) -> str:
    error_html = (
        '<p class="error">That authorization password was not valid.</p>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize Yoke Mooncake</title><style>
body{{font:16px system-ui;background:#111827;color:#f9fafb;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{width:min(440px,calc(100% - 40px));background:#1f2937;padding:28px;border-radius:16px}}
h1{{margin-top:0}} input,button{{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;border-radius:8px}}
button{{background:#f97316;color:white;border:0;font-weight:700;cursor:pointer}} .muted{{color:#9ca3af}} .error{{color:#fca5a5}}
</style></head><body><main><h1>Authorize Yoke Mooncake</h1>
<p><strong>{client_name}</strong> is requesting access to <strong>{scopes}</strong>.</p>
<p class="muted">This permits ChatGPT to read, modify, and run commands as the Mooncake service account.</p>
{error_html}<form method="post" action="/oauth/consent">
<input type="hidden" name="request_id" value="{request_id}">
<label>Authorization password<input type="password" name="password" required autofocus autocomplete="current-password"></label>
<button type="submit">Authorize ChatGPT</button></form></main></body></html>"""


def _header(scope: Scope, name: bytes) -> bytes | None:
    return next((value for key, value in scope.get("headers", []) if key == name), None)


ASGISend = Callable[[Message], Awaitable[None]]
