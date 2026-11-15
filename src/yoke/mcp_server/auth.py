"""Authentication helpers for private and ChatGPT MCP deployments."""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from mcp.server.auth.provider import AuthorizeError, TokenError
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
from starlette.responses import RedirectResponse


from yoke._file_io import atomic_write_text
from yoke.mcp_server.auth_consent import ConsentAttempts, consent_html


_ACCESS_TOKEN_SECONDS = 60 * 60
_REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
_TOKEN_SUBJECT = "yoke-mcp-user"
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
        self._consent_attempts = ConsentAttempts()
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
            raise AuthorizeError(
                error="invalid_target",
                error_description="Unknown MCP resource",
            )
        request_id = secrets.token_urlsafe(32)
        async with self._lock:
            self._prune_expired()
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
        async with self._lock:
            if self._prune_expired():
                self._save_state()
            code = self._authorization_codes.get(authorization_code)
            return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self._lock:
            self._prune_expired()
            stored = self._authorization_codes.pop(authorization_code.code, None)
            if stored is None or stored.client_id != client.client_id:
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
        async with self._lock:
            if self._prune_expired():
                self._save_state()
            token = self._refresh_tokens.get(refresh_token)
            return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._lock:
            self._prune_expired()
            stored = self._refresh_tokens.pop(refresh_token.token, None)
            if stored is None or stored.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Refresh token is invalid or already used",
                )
            token = self._issue_tokens(client.client_id, scopes)
            self._save_state()
            return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self._lock:
            if self._prune_expired():
                self._save_state()
            return self._access_tokens.get(token)

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
            consent_html(client_name, scopes, escaped_request_id),
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
        async with self._lock:
            self._prune_expired()
            pending = self._pending.get(request_id)
            if pending is None:
                return HTMLResponse("Authorization request expired", status_code=400)
            retry_after = self._consent_attempts.retry_after(pending.client_id)
            if retry_after:
                return HTMLResponse(
                    "Too many authorization attempts. Try again later.",
                    status_code=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "Cache-Control": "no-store",
                    },
                )
            if not hmac.compare_digest(
                password.encode(), self.authorization_password.encode()
            ):
                self._consent_attempts.failed(pending.client_id)
                return HTMLResponse(
                    consent_html(
                        "ChatGPT",
                        "Yoke tools",
                        html.escape(request_id, quote=True),
                        error=True,
                    ),
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
            self._consent_attempts.succeeded(pending.client_id)
            self._pending.pop(request_id)
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
                subject=_TOKEN_SUBJECT,
            )
        query = f"code={code_value}"
        if pending.params.state is not None:
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
            subject=_TOKEN_SUBJECT,
        )
        self._refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _REFRESH_TOKEN_SECONDS,
            subject=_TOKEN_SUBJECT,
        )
        return OAuthToken(
            access_token=access_value,
            expires_in=_ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    def _prune_expired(self) -> bool:
        now = time.time()
        changed = False
        for records in (
            self._pending,
            self._authorization_codes,
            self._access_tokens,
            self._refresh_tokens,
        ):
            for key, value in list(records.items()):
                if value.expires_at is not None and value.expires_at <= now:
                    del records[key]
                    changed = True
        return changed

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text())
        self._clients = {
            key: OAuthClientInformationFull.model_validate(value)
            for key, value in data.get("clients", {}).items()
        }
        access_tokens = {
            key: AccessToken.model_validate(value)
            for key, value in data.get("access_tokens", {}).items()
        }
        refresh_tokens = {
            key: RefreshToken.model_validate(value)
            for key, value in data.get("refresh_tokens", {}).items()
        }
        needs_subject_migration = any(
            token.subject != _TOKEN_SUBJECT
            for token in (*access_tokens.values(), *refresh_tokens.values())
        )
        self._access_tokens = {
            key: token.model_copy(update={"subject": _TOKEN_SUBJECT})
            for key, token in access_tokens.items()
        }
        self._refresh_tokens = {
            key: token.model_copy(update={"subject": _TOKEN_SUBJECT})
            for key, token in refresh_tokens.items()
        }
        if self._prune_expired() or needs_subject_migration:
            self._save_state()

    def _save_state(self) -> None:
        self._prune_expired()
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
        atomic_write_text(self.state_file, json.dumps(payload, separators=(",", ":")))
