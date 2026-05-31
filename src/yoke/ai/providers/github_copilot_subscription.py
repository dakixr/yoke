"""GitHub Copilot subscription provider plugin for the YOKE harness."""

# ruff: noqa: ANN401,D101,D102,D103,E501,S105

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from yoke.agent.models import Message, MessagePhase, Role, ToolCall
from yoke.ai.providers.base import (
    Provider,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.openai_compat import serialize_message_for_openai
from yoke.ai.providers.usage import parse_token_usage
from pydantic import BaseModel, Field, ValidationError, field_validator

PROVIDER_NAME = "copilot"

OAUTH_PROVIDER_ID = "github-copilot"
CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode("ascii")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_BASE_URL = "https://api.individual.githubcopilot.com"
MODEL_ENABLEMENT_IDS = (
    "claude-opus-4.7",
    "claude-sonnet-4.6",
    "gemini-3.1-pro-preview",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "grok-code-fast-1",
    "claude-sonnet-4.5",
    "claude-opus-4.5",
    "claude-haiku-4.5",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "oswe-vscode-prime",
    "gpt-5.2",
    "gpt-4.1",
    "gpt-4o",
)
MODEL_CATALOG = (
    # ProviderModelInfo(
    #     id="gpt-5.5",
    #     display_name="GPT-5.5",
    #     context_window_tokens=272_000,
    #     thinking_levels=("none", "minimal", "low", "medium", "high", "xhigh"),
    #     supports_image_inputs=True,
    # ),
    ProviderModelInfo(
        id="gpt-5.4",
        display_name="GPT-5.4",
        context_window_tokens=272_000,
        thinking_levels=("none", "minimal", "low", "medium", "high", "xhigh"),
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    # ProviderModelInfo(
    #     id="gpt-5.4-mini",
    #     display_name="GPT-5.4 mini",
    #     context_window_tokens=272_000,
    #     thinking_levels=("none", "minimal", "low", "medium", "high", "xhigh"),
    #     supports_image_inputs=True,
    # ),
)
COPILOT_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}


def list_provider_models(context: Any) -> list[ProviderModelInfo]:
    del context
    return [model.model_copy(deep=True) for model in MODEL_CATALOG]


def register_provider(context: Any) -> GitHubCopilotProvider:
    env = context.env or os.environ
    return GitHubCopilotProvider(
        GitHubCopilotConfig(
            auth_path=(
                Path(env.get("YOKE_COPILOT_AUTH_PATH", ""))
                if env.get("YOKE_COPILOT_AUTH_PATH")
                else context.home / ".yoke" / "auth.json"
            ),
            model=(context.model or env.get("YOKE_COPILOT_MODEL") or "gpt-5.4"),
            timeout_seconds=float(env.get("YOKE_COPILOT_TIMEOUT_SECONDS") or "600"),
            max_retries=int(env.get("YOKE_COPILOT_MAX_RETRIES") or "5"),
        )
    )


class GitHubCopilotConfig(BaseModel):
    auth_path: Path
    model: str = "gpt-5.5"
    timeout_seconds: float = 600.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 60.0


@dataclass(slots=True)
class CopilotCredentials:
    access: str
    refresh: str
    expires: int
    enterprise_url: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> CopilotCredentials:
        access = payload.get("access")
        refresh = payload.get("refresh")
        expires = payload.get("expires")
        enterprise_url = payload.get("enterpriseUrl")
        if not isinstance(access, str) or not access:
            raise ValueError("Stored Copilot auth is missing an access token.")
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("Stored Copilot auth is missing a GitHub token.")
        if not isinstance(expires, int | float):
            raise ValueError("Stored Copilot auth is missing expiry metadata.")
        if not isinstance(enterprise_url, str) or not enterprise_url:
            enterprise_url = None
        return cls(
            access=access,
            refresh=refresh,
            expires=int(expires),
            enterprise_url=enterprise_url,
        )

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "oauth",
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
        }
        if self.enterprise_url:
            payload["enterpriseUrl"] = self.enterprise_url
        return payload


class GitHubCopilotProvider(Provider):
    provider_name = PROVIDER_NAME
    supports_image_inputs = True

    def __init__(
        self,
        config: GitHubCopilotConfig,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._sleep = sleep or time.sleep
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=config.timeout_seconds,
            verify=False,  # noqa: S501
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

    def current_model_id(self) -> str | None:
        model = self.config.model.strip()
        return model or None

    def current_model_info(self) -> ProviderModelInfo | None:
        current_model = self.current_model_id()
        if current_model is None:
            return None
        for model in self.list_models():
            if model.id == current_model:
                return model
        return None

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        del reasoning_effort
        normalized_model = model_id.strip()
        if not normalized_model:
            raise ValueError("model_id must be a non-empty string")
        allowed_models = {model.id for model in self.list_models()}
        if normalized_model not in allowed_models:
            choices = ", ".join(sorted(allowed_models))
            raise ValueError(
                f"Unknown model {normalized_model!r} for provider 'copilot'. "
                f"Available: {choices}."
            )
        self.config.model = normalized_model

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        credentials = self._fresh_credentials()
        payload = self._request_payload(messages, tools)
        headers = self._request_headers(messages, credentials)
        last_error: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.post(
                    self._chat_completions_url(credentials),
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError("Copilot request timed out.")
                if attempt < self.config.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                raise ProviderError(f"Copilot request failed: {exc}") from exc

            if response.status_code in {401, 403} and attempt == 0:
                credentials = self._force_refresh_credentials(credentials)
                headers = self._request_headers(messages, credentials)
                continue
            if response.status_code == 429:
                retry_after = retry_after_seconds(response)
                last_error = ProviderRateLimitError(
                    f"Copilot request was rate limited: {error_detail(response)}",
                    retry_after_seconds=retry_after,
                )
                if attempt < self.config.max_retries:
                    self._sleep(retry_after or self._backoff_seconds(attempt))
                    continue
                raise last_error
            if 500 <= response.status_code < 600:
                last_error = ProviderServerError(
                    f"Copilot server error: {error_detail(response)}",
                    status_code=response.status_code,
                )
                if attempt < self.config.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error
            if response.is_error:
                raise ProviderError(
                    f"Copilot request failed: {error_detail(response)}",
                    status_code=response.status_code,
                )
            return parse_chat_completion_response(response)
        if last_error is not None:
            raise last_error
        raise ProviderError("Copilot request failed unexpectedly.")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _fresh_credentials(self) -> CopilotCredentials:
        storage = AuthStorage(self.config.auth_path)
        credentials = storage.get_oauth(OAUTH_PROVIDER_ID)
        if credentials is None:
            credentials = login_github_copilot()
            storage.set_oauth(OAUTH_PROVIDER_ID, credentials)
            return credentials
        if credentials.expires - int(time.time() * 1000) > 60_000:
            return credentials
        return storage.refresh_oauth_with_lock(
            OAUTH_PROVIDER_ID,
            refresh_github_copilot_token,
        )

    def _force_refresh_credentials(
        self, credentials: CopilotCredentials
    ) -> CopilotCredentials:
        refreshed = refresh_github_copilot_token(credentials)
        AuthStorage(self.config.auth_path).set_oauth(OAUTH_PROVIDER_ID, refreshed)
        return refreshed

    def _request_payload(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [serialize_message_for_openai(message) for message in messages],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _request_headers(
        self, messages: list[Message], credentials: CopilotCredentials
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credentials.access}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            **COPILOT_HEADERS,
            **build_dynamic_copilot_headers(messages),
        }
        return headers

    def _chat_completions_url(self, credentials: CopilotCredentials) -> str:
        return f"{copilot_base_url(credentials).rstrip('/')}/chat/completions"

    def _backoff_seconds(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )


class AuthStorage:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def get_oauth(self, provider_id: str) -> CopilotCredentials | None:
        payload = self._read()
        raw = payload.get(provider_id)
        if not isinstance(raw, dict):
            return None
        if raw.get("type") != "oauth":  # ty:ignore[invalid-argument-type]
            return None
        return CopilotCredentials.from_json(raw)  # ty:ignore[invalid-argument-type]

    def set_oauth(self, provider_id: str, credentials: CopilotCredentials) -> None:
        with self._lock():
            payload = self._read()
            payload[provider_id] = credentials.to_json()
            self._write(payload)

    def refresh_oauth_with_lock(
        self,
        provider_id: str,
        refresher: Callable[[CopilotCredentials], CopilotCredentials],
    ) -> CopilotCredentials:
        with self._lock():
            current = self.get_oauth(provider_id)
            if current is None:
                current = login_github_copilot()
                self._write_provider(provider_id, current)
                return current
            if current.expires - int(time.time() * 1000) > 60_000:
                return current
            try:
                refreshed = refresher(current)
            except Exception:
                reread = self.get_oauth(provider_id)
                if (
                    reread is not None
                    and reread.expires - int(time.time() * 1000) > 60_000
                ):
                    return reread
                raise
            self._write_provider(provider_id, refreshed)
            return refreshed

    def _write_provider(
        self, provider_id: str, credentials: CopilotCredentials
    ) -> None:
        payload = self._read()
        payload[provider_id] = credentials.to_json()
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Unable to parse Copilot auth file {self.path}."
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Copilot auth file {self.path} is invalid.")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @contextlib.contextmanager
    def _lock(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30
        handle: int | None = None
        while handle is None:
            try:
                handle = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"Timed out waiting for auth lock {self.lock_path}."
                    ) from None
                time.sleep(0.1)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()


@dataclass(slots=True)
class CopilotUrls:
    device_code_url: str
    access_token_url: str
    copilot_token_url: str


@dataclass(slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class CopilotMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: object) -> MessagePhase | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"commentary", "preamble"}:
            return "commentary"
        if normalized in {"final_answer", "final"}:
            return "final_answer"
        return None

    def to_message(self) -> Message:
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            phase=self.phase,
        )


class CopilotChoice(BaseModel):
    message: CopilotMessage


class CopilotChatCompletionResponse(BaseModel):
    choices: list[CopilotChoice]
    usage: dict[str, object] | None = None


def login_github_copilot() -> CopilotCredentials:
    domain = "github.com"
    device = start_device_flow(domain)
    print("Open this URL to authorize GitHub Copilot:")
    print(device.verification_uri)
    print(f"Enter this code: {device.user_code}")
    with contextlib.suppress(Exception):
        webbrowser.open(device.verification_uri)
    github_token = poll_for_github_token(domain, device)
    credentials = exchange_copilot_token(github_token, None)
    enable_known_models(credentials)
    return credentials


def start_device_flow(domain: str) -> DeviceCode:
    response = httpx.post(
        urls_for_domain(domain).device_code_url,
        data={"client_id": CLIENT_ID, "scope": "read:user"},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": COPILOT_HEADERS["User-Agent"],
        },
        timeout=60,
        verify=False,  # noqa: S501
    )
    if response.is_error:
        raise ProviderError(f"GitHub device login failed: {error_detail(response)}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ProviderError("GitHub device login returned invalid payload.")
    try:
        return DeviceCode(
            device_code=required_str(payload, "device_code"),
            user_code=required_str(payload, "user_code"),
            verification_uri=required_str(payload, "verification_uri"),
            interval=int(payload.get("interval") or 5),
            expires_in=int(payload.get("expires_in") or 900),
        )
    except (TypeError, ValueError) as exc:
        raise ProviderError("GitHub device login missed required fields.") from exc


def poll_for_github_token(domain: str, device: DeviceCode) -> str:
    deadline = time.monotonic() + device.expires_in
    interval = max(float(device.interval), 1.0)
    while time.monotonic() < deadline:
        time.sleep(interval)
        response = httpx.post(
            urls_for_domain(domain).access_token_url,
            data={
                "client_id": CLIENT_ID,
                "device_code": device.device_code,
                "grant_type": DEVICE_GRANT,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": COPILOT_HEADERS["User-Agent"],
            },
            timeout=60,
            verify=False,  # noqa: S501
        )
        payload = response.json()
        if isinstance(payload, dict):
            access_token = payload.get("access_token")
            if isinstance(access_token, str) and access_token:
                return access_token
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            description = payload.get("error_description")
            detail = description if isinstance(description, str) else error
            raise ProviderError(f"GitHub device login failed: {detail}")
        if response.is_error:
            raise ProviderError(f"GitHub device login failed: {error_detail(response)}")
    raise ProviderError(
        "GitHub device login timed out. Retry login and check local clock drift "
        "if this keeps happening."
    )


def refresh_github_copilot_token(
    credentials: CopilotCredentials,
) -> CopilotCredentials:
    return exchange_copilot_token(credentials.refresh, credentials.enterprise_url)


def exchange_copilot_token(
    github_token: str, enterprise_domain: str | None
) -> CopilotCredentials:
    domain = enterprise_domain or "github.com"
    response = httpx.get(
        urls_for_domain(domain).copilot_token_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {github_token}",
            **COPILOT_HEADERS,
        },
        timeout=60,
        verify=False,  # noqa: S501
    )
    if response.is_error:
        raise ProviderError(f"Copilot token exchange failed: {error_detail(response)}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ProviderError("Copilot token endpoint returned invalid payload.")
    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not token:
        raise ProviderError("Copilot token endpoint missed token.")
    if not isinstance(expires_at, int | float):
        raise ProviderError("Copilot token endpoint missed expires_at.")
    return CopilotCredentials(
        access=token,
        refresh=github_token,
        expires=int(float(expires_at) * 1000 - 5 * 60 * 1000),
        enterprise_url=enterprise_domain,
    )


def enable_known_models(credentials: CopilotCredentials) -> None:
    base_url = copilot_base_url(credentials).rstrip("/")
    for model_id in MODEL_ENABLEMENT_IDS:
        with contextlib.suppress(Exception):
            httpx.post(
                f"{base_url}/models/{model_id}/policy",
                json={"state": "enabled"},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {credentials.access}",
                    "Content-Type": "application/json",
                    **COPILOT_HEADERS,
                },
                timeout=30,
                verify=False,  # noqa: S501
            )


def urls_for_domain(domain: str) -> CopilotUrls:
    return CopilotUrls(
        device_code_url=f"https://{domain}/login/device/code",
        access_token_url=f"https://{domain}/login/oauth/access_token",
        copilot_token_url=f"https://api.{domain}/copilot_internal/v2/token",
    )


def copilot_base_url(credentials: CopilotCredentials) -> str:
    match = re.search(r"proxy-ep=([^;]+)", credentials.access)
    if match:
        return f"https://{match.group(1).replace('proxy.', 'api.', 1)}"
    if credentials.enterprise_url:
        return f"https://copilot-api.{credentials.enterprise_url}"
    return DEFAULT_BASE_URL


def build_dynamic_copilot_headers(messages: list[Message]) -> dict[str, str]:
    headers = {
        "X-Initiator": "user" if last_role(messages) == "user" else "agent",
        "Openai-Intent": "conversation-panel",
    }
    if any(message.has_image_inputs() for message in messages):
        headers["Copilot-Vision-Request"] = "true"
    return headers


def last_role(messages: list[Message]) -> str | None:
    for message in reversed(messages):
        if message.role != "system":
            return message.role
    return None


def parse_chat_completion_response(response: httpx.Response) -> Message:
    try:
        completion = CopilotChatCompletionResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise ProviderError("Copilot returned an invalid response payload.") from exc
    if not completion.choices:
        raise ProviderError("Copilot returned no completion choices.")
    message = completion.choices[0].message.to_message()
    message.usage = parse_token_usage(
        completion.usage,
        provider_name=PROVIDER_NAME,
        model_id=None,
    )
    return message


def required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return response.text.strip() or f"HTTP {response.status_code}"


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
