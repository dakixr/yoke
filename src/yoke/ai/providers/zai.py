from __future__ import annotations

# ruff: noqa

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from yoke.agent.models import Message, MessagePhase, Role, ToolCall, ToolFunction
from yoke.ai.providers.base import (
    Provider,
    ProviderCancelledError,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
    sleep_with_cancel,
)
from yoke.ai.providers.openai_compat.content import normalize_openai_request_messages
from yoke.ai.providers.usage import parse_token_usage
from pydantic import BaseModel, Field, ValidationError, field_validator

PROVIDER_NAME = "zai"
THINKING_LEVELS = ("none", "thinking")
MODEL_CATALOG = (
    ProviderModelInfo(
        id="glm-5.2",
        display_name="GLM-5.2",
        context_window_tokens=1_000_000,
        thinking_levels=THINKING_LEVELS,
        default_thinking_level="thinking",
        supports_image_inputs=False,
    ),
)


def list_provider_models(context):
    del context
    return [model.model_copy(deep=True) for model in MODEL_CATALOG]


def register_provider(context):
    env = context.env or {}
    api_key = env.get("ZAI_API_KEY", "")
    if not api_key:
        raise ValueError("zai provider requires ZAI_API_KEY.")
    return ZAIProvider(
        ZAIConfig(
            ayoke_key=api_key,
            model=context.model or "glm-5.2",
            reasoning_effort=context.reasoning_effort,
            debug_log_path=env.get("ZAI_DEBUG_LOG_PATH") or None,
        )
    )


class ZAIConfig(BaseModel):
    """Configuration for the native Z.AI coding provider."""

    ayoke_key: str
    model: str = "glm-5.2"
    # This key is for the Z.AI Coding Plan; the regular paas endpoint can
    # reject it even when the token is valid for coding-plan traffic.
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    timeout_seconds: float | None = None
    debug_log_path: str | None = None
    reasoning_effort: str | None = None
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 32.0
    connect_timeout_seconds: float = 10.0
    read_idle_timeout_seconds: float = 60.0
    total_timeout_seconds: float = 180.0


class ZAIResponseMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None
    reasoning_content: str | None = None

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
            reasoning_content=self.reasoning_content,
        )


class ZAIChoice(BaseModel):
    message: ZAIResponseMessage


class ZAIChatCompletionResponse(BaseModel):
    choices: list[ZAIChoice]
    usage: dict[str, object] | None = None


class ZAIProvider(Provider):
    """Provider for Z.AI's coding chat-completions API.

    In addition to transport concerns, this provider normalizes tool-call
    histories to match the message patterns Z.AI accepts.
    """

    provider_name = PROVIDER_NAME
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(
        self,
        config: ZAIConfig,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._sleep = sleep or time.sleep
        self._client = http_client or self._new_client()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                self.config.total_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            ),
            verify=False,
            headers={
                "Authorization": f"Bearer {self.config.ayoke_key}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
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
        return ProviderModelInfo(
            id=current_model,
            display_name=current_model,
            context_window_tokens=128_000,
            thinking_levels=THINKING_LEVELS,
            default_thinking_level="thinking",
            supports_image_inputs=False,
        )

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        normalized_model = model_id.strip()
        if not normalized_model:
            raise ValueError("model_id must be a non-empty string")
        available = {model.id: model for model in self.list_models()}
        if normalized_model not in available:
            choices = ", ".join(sorted(available))
            raise ValueError(
                f"Unknown model {normalized_model!r} for provider 'zai'. "
                f"Available: {choices}."
            )
        if reasoning_effort is not None:
            normalized_reasoning = reasoning_effort.strip().lower()
            if normalized_reasoning not in available[normalized_model].thinking_levels:
                allowed = ", ".join(available[normalized_model].thinking_levels)
                raise ValueError(
                    f"Unsupported reasoning effort {reasoning_effort!r} for "
                    f"model {normalized_model!r}. Allowed: {allowed}."
                )
            self.config.reasoning_effort = normalized_reasoning
        self.config.model = normalized_model

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send one request to Z.AI and return the first completion message."""
        return self._complete_impl(messages, tools, cancel_requested=lambda: False)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        return self._with_request_cancellation(
            lambda: self._complete_impl(
                messages,
                tools,
                cancel_requested=cancel_requested,
            ),
            cancel_requested=cancel_requested,
        )

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        prepared_messages = self._prepare_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                self._message_to_api_dict(message) for message in prepared_messages
            ],
            "stream": True,
        }
        thinking = _thinking_config(self.config.reasoning_effort)
        if thinking is not None:
            payload["thinking"] = thinking
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        stream_timeout = httpx.Timeout(
            self.config.read_idle_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
        )
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"

        last_error: ProviderError | None = None
        attempted_message_recovery = False

        for attempt in range(self.config.max_retries + 1):
            if cancel_requested():
                raise ProviderCancelledError()
            try:
                with self._client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    timeout=stream_timeout,
                ) as response:
                    if response.status_code == 429:
                        response.read()
                        detail = self._error_detail(response)
                        retry_after = self._retry_after_seconds(response)
                        last_error = ProviderRateLimitError(
                            (
                                f"ZAI request failed after {attempt + 1} attempts: {detail}"
                                if attempt == self.config.max_retries
                                else f"ZAI request was rate limited: {detail}"
                            ),
                            retry_after_seconds=retry_after,
                        )
                        if attempt < self.config.max_retries:
                            sleep_with_cancel(
                                retry_after or self._backoff_seconds(attempt),
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if 500 <= response.status_code < 600:
                        response.read()
                        detail = self._error_detail(response)
                        last_error = ProviderServerError(
                            (
                                f"ZAI request failed after {attempt + 1} attempts: {detail}"
                                if attempt == self.config.max_retries
                                else f"ZAI server error: {detail}"
                            ),
                            status_code=response.status_code,
                        )
                        if attempt < self.config.max_retries:
                            sleep_with_cancel(
                                self._backoff_seconds(attempt),
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if response.is_error:
                        response.read()
                        detail = self._error_detail(response)
                        if (
                            not attempted_message_recovery
                            and self._looks_like_illegal_messages_error(detail)
                        ):
                            self._log_debug_event(
                                "illegal_messages_error",
                                detail=detail,
                                messages=[
                                    message.to_api_dict()
                                    for message in prepared_messages
                                ],
                            )
                            recovered_messages = self._recover_illegal_messages(
                                prepared_messages
                            )
                            attempted_message_recovery = True
                            if recovered_messages != prepared_messages:
                                self._log_debug_event(
                                    "illegal_messages_recovery",
                                    detail=detail,
                                    messages=[
                                        message.to_api_dict()
                                        for message in recovered_messages
                                    ],
                                )
                                prepared_messages = recovered_messages
                                payload["messages"] = [
                                    self._message_to_api_dict(message)
                                    for message in prepared_messages
                                ]
                                continue
                        raise ProviderError(
                            f"ZAI request failed: {detail}",
                            status_code=response.status_code,
                        )

                    message = self._parse_sse_response(
                        response,
                        cancel_requested=cancel_requested,
                    )
                    return message
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    (
                        f"ZAI request timed out after {attempt + 1} attempts."
                        if attempt == self.config.max_retries
                        else "ZAI request timed out (server went silent)."
                    )
                )
                if attempt < self.config.max_retries:
                    sleep_with_cancel(
                        self._backoff_seconds(attempt),
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                raise ProviderError(f"ZAI request failed: {exc}") from exc

        if last_error is not None:
            raise last_error
        raise ProviderError("ZAI request failed unexpectedly.")

    def _parse_sse_response(
        self,
        response: httpx.Response,
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Read an SSE chat-completion stream and assemble a final Message."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, object] | None = None
        for line in response.iter_lines():
            if cancel_requested():
                raise ProviderCancelledError()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices") or []
            if choices:
                choice = choices[0]
                delta = choice.get("delta") or {}
                if isinstance(delta, dict):
                    delta_content = delta.get("content")
                    if isinstance(delta_content, str) and delta_content:
                        content_parts.append(delta_content)
                    delta_reasoning = delta.get("reasoning_content")
                    if isinstance(delta_reasoning, str) and delta_reasoning:
                        reasoning_parts.append(delta_reasoning)
                    delta_tool_calls = delta.get("tool_calls")
                    if isinstance(delta_tool_calls, list):
                        self._merge_streaming_tool_calls(tool_calls, delta_tool_calls)
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict):
                usage = chunk_usage
        assembled_tool_calls = [
            ToolCall(
                id=tc["id"],
                type=tc.get("type", "function"),
                function=ToolFunction(
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                ),
            )
            for idx in sorted(tool_calls)
            for tc in [tool_calls[idx]]
            if tc["id"] and tc["function"]["name"]
        ]
        content = "".join(content_parts) or None
        reasoning = "".join(reasoning_parts) or None
        message = Message(
            role="assistant",
            content=content,
            reasoning_content=reasoning,
            tool_calls=assembled_tool_calls or [],
        )
        if usage is not None:
            message.usage = parse_token_usage(
                usage, provider_name=PROVIDER_NAME, model_id=self.config.model
            )
        if not content and not assembled_tool_calls and not reasoning:
            raise ProviderError("ZAI returned an empty streaming completion.")
        return message

    @staticmethod
    def _merge_streaming_tool_calls(
        tool_calls: dict[int, dict[str, Any]],
        delta_tool_calls: list[Any],
    ) -> None:
        for delta in delta_tool_calls:
            if not isinstance(delta, dict):
                continue
            idx = delta.get("index", 0)
            if idx not in tool_calls:
                tool_calls[idx] = {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            existing = tool_calls[idx]
            delta_id = delta.get("id")
            if isinstance(delta_id, str) and delta_id:
                existing["id"] = delta_id
            delta_type = delta.get("type")
            if isinstance(delta_type, str) and delta_type:
                existing["type"] = delta_type
            fn = delta.get("function") or {}
            if isinstance(fn, dict):
                fn_name = fn.get("name")
                if isinstance(fn_name, str) and fn_name:
                    existing["function"]["name"] += fn_name
                fn_args = fn.get("arguments")
                if isinstance(fn_args, str) and fn_args:
                    existing["function"]["arguments"] += fn_args

    def _with_request_cancellation(
        self,
        action: Callable[[], Message],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        if not self._owns_client:
            return action()
        finished = threading.Event()
        client_closed = threading.Event()

        def close_on_cancel() -> None:
            while not finished.wait(0.05):
                if cancel_requested():
                    client_closed.set()
                    self._client.close()
                    return

        threading.Thread(target=close_on_cancel, daemon=True).start()
        try:
            message = action()
            if cancel_requested():
                raise ProviderCancelledError()
            return message
        finally:
            finished.set()
            if client_closed.is_set():
                self._client = self._new_client()

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""

        if self._owns_client:
            self._client.close()

    def _prepare_messages(self, messages: list[Message]) -> list[Message]:
        prepared = normalize_openai_request_messages(messages)
        prepared = self._merge_leading_system_messages(prepared)
        for message in prepared:
            if message.role == "assistant" and message.content is None:
                message.content = ""
        prepared = self._drop_empty_assistant_messages(prepared)
        return prepared

    def _message_to_api_dict(self, message: Message) -> dict[str, object]:
        payload = message.to_api_dict()
        # Z.AI preserved-thinking mode requires complete, unmodified prior
        # reasoning_content. Yoke cannot guarantee that across compaction and
        # transcript transforms, so do not replay it by default.
        payload.pop("reasoning_content", None)
        return payload

    def _merge_leading_system_messages(self, messages: list[Message]) -> list[Message]:
        leading_system_messages: list[Message] = []
        for message in messages:
            if message.role != "system":
                break
            leading_system_messages.append(message)
        if len(leading_system_messages) <= 1:
            return messages
        merged_content = "\n\n".join(
            content
            for message in leading_system_messages
            if (content := _message_text(message))
        )
        return [
            Message.system(merged_content),
            *messages[len(leading_system_messages) :],
        ]

    def _drop_empty_assistant_messages(self, messages: list[Message]) -> list[Message]:
        return [
            message
            for message in messages
            if not (
                message.role == "assistant"
                and not message.tool_calls
                and not _message_text(message)
            )
        ]

    def _looks_like_illegal_messages_error(self, detail: str) -> bool:
        normalized = detail.lower()
        return (
            "messages parameter is illegal" in normalized
            or "messages parameter" in normalized
        )

    def _recover_illegal_messages(self, messages: list[Message]) -> list[Message]:
        recovered: list[Message] = []
        system_messages: list[Message] = []
        index = 0
        while index < len(messages) and messages[index].role == "system":
            system_messages.append(messages[index].model_copy(deep=True))
            index += 1
        if system_messages:
            recovered.extend(self._merge_leading_system_messages(system_messages))
        textual_messages = self._render_tool_messages_as_text(messages[index:])
        recovered.extend(
            self._ensure_recoverable_dialogue_shape(
                self._coalesce_text_messages(textual_messages)
            )
        )
        return recovered

    def _render_tool_messages_as_text(self, messages: list[Message]) -> list[Message]:
        rendered: list[Message] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role == "assistant" and message.tool_calls:
                tool_ids = [tool_call.id for tool_call in message.tool_calls]
                tool_results: list[Message] = []
                lookahead = index + 1
                while lookahead < len(messages):
                    candidate = messages[lookahead]
                    if candidate.role == "tool" and candidate.tool_call_id in tool_ids:
                        tool_results.append(candidate)
                        lookahead += 1
                        continue
                    break
                content = self._render_tool_exchange(message, tool_results)
                if content:
                    rendered.append(Message.assistant(content))
                index = lookahead
                continue
            if message.role in {"user", "assistant"} and _message_text(message):
                rendered.append(
                    Message(
                        role=message.role,
                        content=_message_text(message),
                    )
                )
            index += 1
        return rendered

    def _render_tool_exchange(
        self, assistant_message: Message, tool_results: list[Message]
    ) -> str:
        parts: list[str] = []
        if assistant_content := _message_text(assistant_message):
            parts.append(assistant_content)
        calls = [
            f"{tool_call.function.name}({tool_call.function.arguments})"
            for tool_call in assistant_message.tool_calls
        ]
        if calls:
            parts.append(f"[Assistant tool calls] {'; '.join(calls)}")
        for tool_message in tool_results:
            if tool_content := _message_text(tool_message):
                parts.append(
                    f"[Tool result] {self._truncate_text(tool_content, limit=1_200)}"
                )
        return "\n".join(parts).strip()

    def _coalesce_text_messages(self, messages: list[Message]) -> list[Message]:
        coalesced: list[Message] = []
        for message in messages:
            if (
                coalesced
                and coalesced[-1].role == message.role
                and message.role != "system"
            ):
                merged_content = "\n\n".join(
                    part
                    for part in [
                        _message_text(coalesced[-1]),
                        _message_text(message),
                    ]
                    if part
                )
                coalesced[-1] = Message(role=message.role, content=merged_content)
                continue
            coalesced.append(message)
        return coalesced

    def _ensure_recoverable_dialogue_shape(
        self, messages: list[Message]
    ) -> list[Message]:
        if not messages:
            return [Message.user(self._recovery_prompt())]
        if messages[-1].role != "user":
            return [*messages, Message.user(self._recovery_prompt())]
        return messages

    def _recovery_prompt(self) -> str:
        return "Continue from the prior context and answer the latest request using the tool results already gathered."

    def _truncate_text(self, text: str, *, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _log_debug_event(self, event: str, **payload: object) -> None:
        if not self.config.debug_log_path:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "model": self.config.model,
            **payload,
        }
        try:
            path = Path(self.config.debug_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _backoff_seconds(self, attempt: int) -> float:
        delay = self.config.retry_backoff_seconds * (2**attempt)
        return min(delay, self.config.max_retry_backoff_seconds)

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return max(delay, 0.0)

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if (
                isinstance(error, dict)
                and isinstance(error.get("message"), str)
                and error["message"].strip()
            ):
                return error["message"].strip()
            if isinstance(error, str) and error.strip():
                return error.strip()
        return response.reason_phrase or f"HTTP {response.status_code}"


def _thinking_config(reasoning_effort: str | None) -> dict[str, object] | None:
    if reasoning_effort is None:
        return None
    normalized = reasoning_effort.strip().lower()
    if normalized == "none":
        return {"type": "disabled"}
    if normalized == "thinking":
        return {"type": "enabled", "clear_thinking": True}
    return None


def _message_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    return message.text_content() or ""
