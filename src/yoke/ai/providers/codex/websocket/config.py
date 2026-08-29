"""Configuration and transport contracts for Codex WebSockets."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel

from yoke.agent.models import MessagePhase
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex.subscription import (
    DEFAULT_BASE_URL,
    DEFAULT_CXAUTH_VAULT_NAME,
    DEFAULT_LOGS_DIR,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    DEFAULT_YOKE_ORIGINATOR,
    CodexSubscriptionConfig,
    default_reasoning_effort_for_model_id,
)

if TYPE_CHECKING:
    from yoke.ai.providers.codex.websocket.provider import CodexProvider

PROVIDER_NAME = "codex"
RESPONSES_WEBSOCKETS_BETA = "responses_websockets=2026-02-06"
DEFAULT_WS_BASE_URL = DEFAULT_BASE_URL
STALE_WEBSOCKET_CLOSED_MESSAGE = "Codex WebSocket closed before response.completed."
WEBSOCKET_TIMEOUT_MESSAGE = "Codex WebSocket timed out waiting for response."
X_CODEX_TURN_STATE_HEADER = "x-codex-turn-state"
WEBSOCKET_REQUEST_TYPE = "response.create"
RESPONSES_LITE_CLIENT_METADATA_KEY = (
    "ws_request_header_x_openai_internal_codex_responses_lite"
)


class CodexWebSocketTimeoutError(ProviderError):
    """Raised when an open WebSocket stops delivering response events."""


class CodexPreviousResponseNotFoundError(ProviderError):
    """Raised when the active account cannot access a prior response id."""


def register_provider(context: Any) -> CodexProvider:
    env = context.env or {}
    cxauth_vault = context.home / DEFAULT_CXAUTH_VAULT_NAME
    model = (
        context.model
        or env.get("YOKE_CODEX_MODEL")
        or env.get("YOKE_CODEX_WEBSOCKETS_MODEL")
        or "gpt-5.6-sol"
    )
    from yoke.ai.providers.codex.websocket.provider import CodexProvider

    return CodexProvider(
        CodexConfig(
            auth_path=context.home / ".codex" / "auth.json",
            accounts_dir=cxauth_vault / "accounts",
            auths_path=(
                Path(env.get("YOKE_CODEX_AUTHS_PATH", ""))
                if env.get("YOKE_CODEX_AUTHS_PATH")
                else context.home / ".yoke" / "providers" / "codex-auth" / "auths.json"
            ),
            selection_path=(
                Path(env.get("YOKE_CODEX_SELECTION_PATH", ""))
                if env.get("YOKE_CODEX_SELECTION_PATH")
                else context.home
                / ".yoke"
                / "providers"
                / "codex-auth"
                / "selection.json"
            ),
            selection_ttl_seconds=int(
                env.get("YOKE_CODEX_SELECTION_TTL_SECONDS") or "1800"
            ),
            model=model,
            prompt_cache_key=getattr(context, "session_id", None),
            base_url=_base_url_from_env(env),
            api_key=env.get("YOKE_CODEX_API_KEY") or None,
            originator=(env.get("YOKE_CODEX_ORIGINATOR") or DEFAULT_YOKE_ORIGINATOR),
            timeout_seconds=float(
                env.get("YOKE_CODEX_TIMEOUT_SECONDS")
                or env.get("YOKE_CODEX_WEBSOCKETS_TIMEOUT_SECONDS")
                or str(DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
            ),
            max_retries=int(
                env.get("YOKE_CODEX_MAX_RETRIES")
                or env.get("YOKE_CODEX_WEBSOCKETS_MAX_RETRIES")
                or "5"
            ),
            reasoning_effort=(
                context.reasoning_effort
                or env.get("YOKE_CODEX_REASONING_EFFORT")
                or env.get("YOKE_CODEX_WEBSOCKETS_REASONING_EFFORT")
                or default_reasoning_effort_for_model_id(model)
            ),
            text_verbosity=(
                env.get("YOKE_CODEX_TEXT_VERBOSITY")
                or env.get("YOKE_CODEX_WEBSOCKETS_TEXT_VERBOSITY")
                or "medium"
            ),
            logs_dir=Path(
                env.get("YOKE_CODEX_LOGS_DIR")
                or env.get("YOKE_CODEX_WEBSOCKETS_LOGS_DIR")
                or env.get("YOKE_PROVIDER_LOGS_DIR")
                or str(DEFAULT_LOGS_DIR)
            ),
            websocket_ping_interval_seconds=optional_float_env(
                env.get("YOKE_CODEX_WEBSOCKETS_PING_INTERVAL_SECONDS"),
                default=None,
            ),
            websocket_ping_timeout_seconds=optional_float_env(
                env.get("YOKE_CODEX_WEBSOCKETS_PING_TIMEOUT_SECONDS"),
                default=20.0,
            ),
        )
    )


class CodexConfig(CodexSubscriptionConfig):
    api_key: str | None = None
    websocket_ping_interval_seconds: float | None = None
    websocket_ping_timeout_seconds: float | None = 20.0


def optional_float_env(value: str | None, *, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    if value.lower() in {"none", "off", "false", "0"}:
        return None
    return float(value)


class CodexWebSocketParseState(BaseModel):
    text_parts: list[str]
    snapshot_text_parts: list[str] = []
    function_calls: dict[str, dict[str, str]]
    completed_payload: dict[str, Any] | None = None
    response_id: str | None = None
    usage_payload: object | None = None
    phase: MessagePhase | None = None
    output_items: list[dict[str, Any]] = []


class CodexWebSocketConnection(Protocol):
    def send(self, payload: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


def websocket_url_for_base(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        response_path = path
    elif path.endswith("/codex"):
        response_path = f"{path}/responses"
    elif path.endswith("/v1"):
        response_path = f"{path}/responses"
    else:
        response_path = f"{path}/codex/responses"
    return urlunparse(
        (
            scheme,
            parsed.netloc,
            response_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def base_url_for_domain(domain: str) -> str:
    normalized = domain.strip().rstrip("/")
    if not normalized:
        raise ValueError("Codex domain must be a non-empty URL.")
    if not normalized.startswith(("http://", "https://", "ws://", "wss://")):
        raise ValueError(
            "Codex domain must include http://, https://, ws://, or wss://."
        )
    if normalized.endswith(("/backend-api", "/backend-api/codex", "/v1")):
        return normalized
    if normalized.endswith(("/backend-api/codex/responses", "/v1/responses")):
        return normalized
    return f"{normalized}/backend-api"


def _base_url_from_env(env: dict[str, str]) -> str:
    domain = env.get("YOKE_CODEX_DOMAIN")
    if domain:
        return base_url_for_domain(domain)
    return (
        env.get("YOKE_CODEX_BASE_URL")
        or env.get("YOKE_CODEX_WEBSOCKETS_BASE_URL")
        or DEFAULT_WS_BASE_URL
    )


def ssl_context_for_websocket_url(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    context = ssl.create_default_context()
    return context
