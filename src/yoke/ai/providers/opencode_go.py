"""OpenCode Go provider plugin for the YOKE harness."""

# ruff: noqa: ANN401,D101,D102,D103,E501,S105

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
from yoke.agent.models import (
    Message,
    ToolCall,
    ToolFunction,
)
from yoke.ai.providers.base import (
    Provider,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.model_selection import (
    cloned_model_catalog,
    current_model_id_from_config,
    current_model_info_from_catalog,
    set_config_model_from_catalog,
)
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    _error_detail,
    _retry_after_seconds,
    build_model_catalog,
    normalize_openai_request_messages,
)
from yoke.ai.providers.openai_compat.content import _local_image_to_data_url
from yoke.ai.providers.usage import parse_token_usage
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from pydantic import BaseModel, Field, ValidationError, field_validator

PROVIDER_NAME = "opencode-go"
AUTH_FILE_KEY = "opencode-go"
ENV_API_KEY = "OPENCODE_API_KEY"
OPENAI_BASE_URL = "https://opencode.ai/zen/go/v1"
ANTHROPIC_BASE_URL = "https://opencode.ai/zen/go"

ANTHROPIC_THINKING_LEVELS = ("high", "max")
DEEPSEEK_THINKING_LEVELS = ("low", "medium", "high", "xhigh", "max")
GLM_THINKING_LEVELS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
KIMI_THINKING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
MIMO_THINKING_LEVELS = ("low", "medium", "high")
MINIMAX_THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

MODEL_PROTOCOLS = {
    "minimax-m3": "anthropic",
    "minimax-m2.7": "anthropic",
    "kimi-k2.5": "openai",
    "mimo-v2.5-pro": "openai",
    "glm-5": "openai",
    "mimo-v2.5": "openai",
    "qwen3.7-plus": "anthropic",
    "qwen3.6-plus": "anthropic",
    "glm-5.1": "openai",
    "deepseek-v4-flash": "openai",
    "kimi-k2.7-code": "openai",
    "kimi-k2.6": "openai",
    "deepseek-v4-pro": "openai",
    "minimax-m2.5": "openai",
    "qwen3.5-plus": "anthropic",
    "mimo-v2-omni": "openai",
    "mimo-v2-pro": "openai",
}

MODEL_CATALOG = build_model_catalog(
    ProviderModelInfo(
        id="glm-5.1",
        display_name="GLM-5.1",
        context_window_tokens=202_752,
        thinking_levels=GLM_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="glm-5",
        display_name="GLM-5",
        context_window_tokens=202_752,
        thinking_levels=GLM_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="kimi-k2.7-code",
        display_name="Kimi K2.7 Code",
        context_window_tokens=262_144,
        thinking_levels=KIMI_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="kimi-k2.6",
        display_name="Kimi K2.6",
        context_window_tokens=262_144,
        thinking_levels=KIMI_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="kimi-k2.5",
        display_name="Kimi K2.5",
        context_window_tokens=262_144,
        thinking_levels=KIMI_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro",
        context_window_tokens=1_000_000,
        thinking_levels=DEEPSEEK_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        context_window_tokens=1_000_000,
        thinking_levels=DEEPSEEK_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="mimo-v2.5",
        display_name="MiMo V2.5",
        context_window_tokens=1_000_000,
        thinking_levels=MIMO_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="mimo-v2-omni",
        display_name="MiMo V2 Omni",
        context_window_tokens=262_144,
        thinking_levels=MIMO_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="mimo-v2-pro",
        display_name="MiMo V2 Pro",
        context_window_tokens=1_048_576,
        thinking_levels=MIMO_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="mimo-v2.5-pro",
        display_name="MiMo V2.5 Pro",
        context_window_tokens=1_048_576,
        thinking_levels=MIMO_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="minimax-m3",
        display_name="MiniMax M3",
        context_window_tokens=1_000_000,
        thinking_levels=ANTHROPIC_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="minimax-m2.7",
        display_name="MiniMax M2.7",
        context_window_tokens=204_800,
        thinking_levels=ANTHROPIC_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="minimax-m2.5",
        display_name="MiniMax M2.5",
        context_window_tokens=204_800,
        thinking_levels=MINIMAX_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="qwen3.7-plus",
        display_name="Qwen3.7 Plus",
        context_window_tokens=1_000_000,
        thinking_levels=ANTHROPIC_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="qwen3.6-plus",
        display_name="Qwen3.6 Plus",
        context_window_tokens=262_144,
        thinking_levels=ANTHROPIC_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="qwen3.5-plus",
        display_name="Qwen3.5 Plus",
        context_window_tokens=262_144,
        thinking_levels=ANTHROPIC_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
    ),
)

ALL_THINKING_LEVELS = tuple(
    dict.fromkeys(
        level
        for levels in (
            ANTHROPIC_THINKING_LEVELS,
            DEEPSEEK_THINKING_LEVELS,
            GLM_THINKING_LEVELS,
            KIMI_THINKING_LEVELS,
            MIMO_THINKING_LEVELS,
            MINIMAX_THINKING_LEVELS,
        )
        for level in levels
    )
)


def list_provider_models(context: Any) -> list[ProviderModelInfo]:
    del context
    return cloned_model_catalog(MODEL_CATALOG)


def register_provider(context: Any) -> OpenCodeGoProvider:
    api_key = os.getenv(ENV_API_KEY, "").strip()
    if not api_key:
        raise ValueError(
            f"OpenCode Go API key not found. Please provide it via {ENV_API_KEY} environment variable."
        )
    return OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key=api_key,
            model=_normalize_model_id(context.model or "kimi-k2.6"),
            timeout_seconds=float(
                os.getenv("YOKE_OPENCODE_GO_TIMEOUT_SECONDS") or "600"
            ),
            max_retries=int(os.getenv("YOKE_OPENCODE_GO_MAX_RETRIES") or "5"),
            reasoning_effort=(
                context.reasoning_effort
                or os.getenv("YOKE_OPENCODE_GO_REASONING_EFFORT")
                or None
            ),
        )
    )


class OpenCodeGoConfig(BaseModel):
    api_key: str
    model: str = "kimi-k2.6"
    timeout_seconds: float = 600.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 15.0
    reasoning_effort: str | None = None
    model_catalog: tuple[ProviderModelInfo, ...] = MODEL_CATALOG

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _normalize_model_id(value)

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in ALL_THINKING_LEVELS:
            raise ValueError(
                "reasoning_effort must be one of none, minimal, low, "
                "medium, high, xhigh, or max"
            )
        return normalized


class AnthropicContentBlock(BaseModel):
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class AnthropicMessageResponse(BaseModel):
    content: list[AnthropicContentBlock] = Field(default_factory=list)
    usage: dict[str, object] | None = None

    def to_message(
        self,
        *,
        provider_name: str | None = None,
        model_id: str | None = None,
    ) -> Message:
        text = "\n".join(
            block.text or ""
            for block in self.content
            if block.type == "text" and block.text
        )
        tool_calls = [
            ToolCall(
                id=block.id or "",
                function=ToolFunction(
                    name=block.name or "",
                    arguments=json.dumps(block.input),
                ),
            )
            for block in self.content
            if block.type == "tool_use" and block.id and block.name
        ]
        return Message(
            role="assistant",
            content=text or None,
            tool_calls=tool_calls,
            usage=parse_token_usage(
                self.usage,
                provider_name=provider_name,
                model_id=model_id,
            ),
        )


class OpenCodeGoProvider(Provider):
    provider_name = PROVIDER_NAME
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(
        self,
        config: OpenCodeGoConfig,
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
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._openai_provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key=config.api_key,
                model=config.model,
                base_url=OPENAI_BASE_URL,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                retry_backoff_seconds=config.retry_backoff_seconds,
                max_retry_backoff_seconds=config.max_retry_backoff_seconds,
                reasoning_effort=config.reasoning_effort,
                provider_name=PROVIDER_NAME,
                model_catalog=config.model_catalog,
            ),
            http_client=http_client,
            sleep=self._sleep,
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return cloned_model_catalog(self.config.model_catalog)

    def current_model_id(self) -> str | None:
        return current_model_id_from_config(self.config)

    def current_model_info(self) -> ProviderModelInfo | None:
        return current_model_info_from_catalog(self.config, self.list_models())

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        set_config_model_from_catalog(
            self.config,
            self.list_models(),
            provider_name=PROVIDER_NAME,
            model_id=_normalize_model_id(model_id),
            reasoning_effort=reasoning_effort,
        )
        self._sync_openai_config()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self._sync_openai_config()
        if MODEL_PROTOCOLS.get(self.config.model) == "anthropic":
            return self._complete_anthropic(messages, tools)
        return self._openai_provider.complete(messages, tools)

    def close(self) -> None:
        self._openai_provider.close()
        if self._owns_client:
            self._client.close()

    def _sync_openai_config(self) -> None:
        self._openai_provider.config.model = self.config.model
        self._openai_provider.config.reasoning_effort = self.config.reasoning_effort

    def _complete_anthropic(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": _max_output_tokens(self.config.model),
            "messages": _serialize_messages_for_anthropic(messages),
        }
        thinking = _anthropic_thinking_config(
            self.config.model,
            self.config.reasoning_effort,
        )
        if thinking:
            payload["thinking"] = thinking
        system = _system_prompt_for_anthropic(messages)
        if system:
            payload["system"] = system
        anthropic_tools = _serialize_tools_for_anthropic(tools)
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        last_error: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.post(
                    f"{ANTHROPIC_BASE_URL}/v1/messages",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "x-api-key": self.config.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError("OpenCode Go request timed out.")
                if attempt < self.config.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                raise ProviderError(f"OpenCode Go request failed: {exc}") from exc

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                last_error = ProviderRateLimitError(
                    f"OpenCode Go request was rate limited: {_error_detail(response)}",
                    retry_after_seconds=retry_after,
                )
                if attempt < self.config.max_retries:
                    self._sleep(self._sleep_seconds(attempt, retry_after))
                    continue
                raise last_error
            if 500 <= response.status_code < 600:
                last_error = ProviderServerError(
                    f"OpenCode Go server error: {_error_detail(response)}",
                    status_code=response.status_code,
                )
                if attempt < self.config.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error
            if response.is_error:
                raise ProviderError(
                    f"OpenCode Go request failed: {_error_detail(response)}",
                    status_code=response.status_code,
                )
            try:
                return AnthropicMessageResponse.model_validate(
                    response.json()
                ).to_message(
                    provider_name=PROVIDER_NAME,
                    model_id=self.config.model,
                )
            except (ValueError, ValidationError) as exc:
                raise ProviderError(
                    "OpenCode Go returned an invalid response payload."
                ) from exc

        if last_error is not None:
            raise last_error
        raise ProviderError("OpenCode Go request failed unexpectedly.")

    def _backoff_seconds(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )

    def _sleep_seconds(
        self, attempt: int, retry_after_seconds: float | None = None
    ) -> float:
        return min(
            retry_after_seconds or self._backoff_seconds(attempt),
            self.config.max_retry_backoff_seconds,
        )


def _resolve_api_key(
    *,
    explicit_key: str,
    auth_path: Path,
    env: Any,
) -> str:
    if explicit_key.strip():
        return explicit_key.strip()
    stored_key = _stored_api_key(auth_path)
    if stored_key:
        if stored_key in env and env.get(stored_key, "").strip():
            return env.get(stored_key, "").strip()
        return stored_key
    env_key = env.get(ENV_API_KEY, "") if hasattr(env, "get") else ""
    if env_key.strip():
        return env_key.strip()
    return ""


def _stored_api_key(auth_path: Path) -> str:
    if not auth_path.is_file():
        return ""
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    credential = payload.get(AUTH_FILE_KEY)
    if not isinstance(credential, dict):
        return ""
    if credential.get("type") not in {"api", "api_key"}:
        return ""
    key = credential.get("key")
    return key.strip() if isinstance(key, str) else ""


def _normalize_model_id(model_id: str) -> str:
    normalized = model_id.strip()
    prefix = f"{PROVIDER_NAME}/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix) :]
    return normalized


def _max_output_tokens(model_id: str) -> int:
    outputs = {
        "minimax-m3": 65_536,
        "minimax-m2.7": 131_072,
        "qwen3.7-plus": 65_536,
        "qwen3.6-plus": 65_536,
        "qwen3.5-plus": 65_536,
    }
    return outputs.get(model_id, 65_536)


def _anthropic_thinking_config(
    model_id: str,
    reasoning_effort: str | None,
) -> dict[str, object] | None:
    if MODEL_PROTOCOLS.get(model_id) != "anthropic":
        return None
    level = (reasoning_effort or "high").strip().lower()
    output_tokens = _max_output_tokens(model_id)
    budget_tokens = 16_000 if level == "high" else 31_999
    return {
        "type": "enabled",
        "budget_tokens": min(budget_tokens, output_tokens - 1),
    }


def _system_prompt_for_anthropic(messages: list[Message]) -> str:
    parts = [
        message.text_content() or ""
        for message in messages
        if message.role == "system" and message.text_content()
    ]
    return "\n\n".join(parts)


def _serialize_messages_for_anthropic(
    messages: list[Message],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for message in normalize_openai_request_messages(messages):
        if message.role == "system":
            continue
        if message.role == "tool":
            serialized.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.text_content() or "",
                        }
                    ],
                }
            )
            continue
        content = _anthropic_content_blocks(message)
        role = "assistant" if message.role == "assistant" else "user"
        serialized.append({"role": role, "content": content})
    return serialized


def _anthropic_content_blocks(message: Message) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    content = message.content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, MessageTextContentPart):
                if part.text:
                    blocks.append({"type": "text", "text": part.text})
                continue
            if isinstance(part, MessageImageURLContentPart):
                blocks.extend(
                    _anthropic_image_blocks(
                        image_url=part.image_url.url,
                        label=None,
                    )
                )
                continue
            if isinstance(part, MessageLocalImageContentPart):
                blocks.extend(
                    _anthropic_image_blocks(
                        image_url=_local_image_to_data_url(part.path),
                        label=part.display_label,
                    )
                )
    else:
        text = message.text_content()
        if text:
            blocks.append({"type": "text", "text": text})
    if message.role == "assistant":
        blocks.extend(
            cast(
                dict[str, object],
                {
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "input": _json_arguments(tool_call.function.arguments),
                },
            )
            for tool_call in message.tool_calls
        )
    if blocks:
        return blocks
    return [{"type": "text", "text": ""}]


def _anthropic_image_blocks(
    *,
    image_url: str,
    label: str | None,
) -> list[dict[str, object]]:
    opening = "<image>" if label is None else f"<image name={label}>"
    source = _anthropic_image_source(image_url)
    return [
        {"type": "text", "text": opening},
        {"type": "image", "source": source},
        {"type": "text", "text": "</image>"},
    ]


def _anthropic_image_source(image_url: str) -> dict[str, object]:
    data_url_prefix = "data:"
    base64_marker = ";base64,"
    if image_url.startswith(data_url_prefix) and base64_marker in image_url:
        media_type, data = image_url[len(data_url_prefix) :].split(
            base64_marker,
            maxsplit=1,
        )
        return {"type": "base64", "media_type": media_type, "data": data}
    return {"type": "url", "url": image_url}


def _json_arguments(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_tools_for_anthropic(
    tools: list[dict[str, object]],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        function = cast(dict[str, object], function)
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        input_schema = function.get("parameters")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        description = function.get("description")
        payload: dict[str, object] = {
            "name": name,
            "input_schema": input_schema,
        }
        if isinstance(description, str) and description.strip():
            payload["description"] = description
        serialized.append(payload)
    return serialized
