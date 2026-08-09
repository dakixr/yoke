"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass

import httpx

from yoke.ai.providers.base import (
    ProviderError,
)

from .catalog import (
    AUTHORIZE_URL,
    CLIENT_ID,
    JWT_CLAIM_PATH,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)
from .helpers import error_detail, first_query_value
from .models import OAuthCredentials


@dataclass(slots=True)
class AuthorizationFlow:
    url: str
    verifier: str
    state: str


def login_openai_codex(originator: str) -> OAuthCredentials:
    flow = create_authorization_flow(originator)
    print("Open this URL to sign in with your ChatGPT Codex subscription:")
    print(flow.url)
    with contextlib.suppress(Exception):
        webbrowser.open(flow.url)
    callback = wait_for_callback(flow.state)
    if callback is None:
        print("Paste the full redirect URL or authorization code below.")
        callback = parse_authorization_input(input("Authorization: "), flow.state)
    return exchange_authorization_code(callback, flow.verifier)


def create_authorization_flow(originator: str) -> AuthorizationFlow:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return AuthorizationFlow(
        url=f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        verifier=verifier,
        state=state,
    )


@dataclass(slots=True)
class AuthorizationCallback:
    code: str
    state: str | None = None


def wait_for_callback(expected_state: str) -> AuthorizationCallback | None:
    host = os.getenv("YOKE_OAUTH_CALLBACK_HOST", "127.0.0.1")
    parsed = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed.port or 1455
    route = parsed.path
    result: dict[str, AuthorizationCallback | Exception] = {}
    done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args
            return

        def do_GET(self) -> None:  # noqa: N802
            request = urllib.parse.urlparse(self.path)
            if request.path != route:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(request.query)
            code = first_query_value(query, "code")
            state = first_query_value(query, "state")
            if not code:
                result["value"] = ProviderError("OAuth callback missed code.")
                self._html(400, "Codex login failed. Missing code.")
                done.set()
                return
            if state != expected_state:
                result["value"] = ProviderError("OAuth state mismatch.")
                self._html(400, "Codex login failed. State mismatch.")
                done.set()
                return
            result["value"] = AuthorizationCallback(code=code, state=state)
            self._html(200, "Codex login complete. You can close this tab.")
            done.set()

        def _html(self, status: int, message: str) -> None:
            body = (
                f"<!doctype html><html><body><p>{message}</p></body></html>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = http.server.HTTPServer((host, port), CallbackHandler)
    except OSError:
        return None
    server.timeout = 0.2

    def serve() -> None:
        while not done.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        return None
    finally:
        server.server_close()
    value = result.get("value")
    if isinstance(value, Exception):
        raise value
    return value


def parse_authorization_input(
    raw_value: str, expected_state: str
) -> AuthorizationCallback:
    value = raw_value.strip()
    if not value:
        raise ProviderError("Authorization input was empty.")
    if "#" in value and not value.startswith("http"):
        code, state = value.split("#", 1)
        callback = AuthorizationCallback(code=code.strip(), state=state.strip())
    elif value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        query = urllib.parse.parse_qs(parsed.query)
        callback = AuthorizationCallback(
            code=first_query_value(query, "code") or "",
            state=first_query_value(query, "state"),
        )
    elif value.startswith("code=") or "&code=" in value:
        query = urllib.parse.parse_qs(value.lstrip("?"))
        callback = AuthorizationCallback(
            code=first_query_value(query, "code") or "",
            state=first_query_value(query, "state"),
        )
    else:
        callback = AuthorizationCallback(code=value)
    if not callback.code:
        raise ProviderError("Authorization input did not include a code.")
    if callback.state is not None and callback.state != expected_state:
        raise ProviderError("OAuth state mismatch.")
    return callback


def exchange_authorization_code(
    callback: AuthorizationCallback, verifier: str
) -> OAuthCredentials:
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": callback.code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    return token_request(body)


def refresh_openai_codex_token(
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": credentials.refresh,
        "client_id": CLIENT_ID,
    }
    return token_request(body)


def token_request(body: dict[str, str]) -> OAuthCredentials:
    try:
        response = httpx.post(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
        )
    except httpx.RequestError as exc:
        raise ProviderError(f"Codex token request failed: {exc}") from exc
    if response.is_error:
        raise ProviderError(f"Codex token request failed: {error_detail(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("Codex token endpoint returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("Codex token endpoint returned invalid payload.")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access, str) or not access:
        raise ProviderError("Codex token endpoint missed access_token.")
    if not isinstance(refresh, str) or not refresh:
        raise ProviderError("Codex token endpoint missed refresh_token.")
    if not isinstance(expires_in, int | float):
        raise ProviderError("Codex token endpoint missed expires_in.")
    account_id = account_id_from_access_token(access)
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=int(time.time() * 1000 + float(expires_in) * 1000),
        account_id=account_id,
    )


def account_id_from_access_token(access_token: str) -> str:
    try:
        payload_segment = access_token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Unable to decode Codex access token.") from exc
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    raise ProviderError("Codex access token does not include an account ID.")
