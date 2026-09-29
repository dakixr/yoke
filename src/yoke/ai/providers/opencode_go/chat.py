"""OpenCode Go streaming chat transport behind the existing completion API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from yoke.agent.message_sanitizer import sanitize_json_surrogates
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError, ProviderError
from yoke.ai.providers.openai_compat.content import (
    normalize_openai_request_messages,
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.events import emit_recovery_event
from yoke.ai.providers.openai_compat.models import OpenAICompatibleResponseMessage
from yoke.ai.providers.openai_compat.provider import OpenAICompatibleProvider
from yoke.ai.providers.usage import parse_token_usage


class GoChatProvider(OpenAICompatibleProvider):
    """Keep Go's wire protocol out of the shared OpenAI-compatible provider."""

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                serialize_message_for_openai(message)
                for message in normalize_openai_request_messages(messages)
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        # OpenCode's GLM variants are empty: the service controls reasoning.
        if self.config.model != "glm-5.3-flash" and self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        payload = sanitize_json_surrogates(payload)
        for attempt in range(self.config.max_retries + 1):
            if cancel_requested():
                raise ProviderCancelledError()
            started = False
            try:
                with self._client.get_client().stream(
                    "POST",
                    self._chat_completions_url(),
                    json=payload,
                    headers=self._headers,
                ) as response:
                    if response.is_error:
                        response.read()
                        if (
                            self._handle_error_response(
                                response,
                                attempt=attempt,
                                cancel_requested=cancel_requested,
                            )
                            is not None
                        ):
                            continue
                    started = True
                    message = read_completion(
                        response, cancel_requested, self.config.model
                    )
                    emit_recovery_event(
                        provider_name=self.provider_name,
                        model_id=self.config.model,
                        attempts=attempt,
                    )
                    return message
            except httpx.HTTPError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                # Do not replay a generation after receiving a successful stream.
                if started or isinstance(exc, httpx.ReadTimeout):
                    raise ProviderError("OpenCode Go stream interrupted.") from exc
                if (
                    isinstance(exc, httpx.RequestError)
                    and self._handle_request_error(
                        exc, attempt=attempt, cancel_requested=cancel_requested
                    )
                    is not None
                ):
                    continue
                raise ProviderError("OpenCode Go connection failed.") from exc
        raise ProviderError("OpenCode Go retry limit reached.")


def read_completion(
    response: httpx.Response, cancel_requested: Callable[[], bool], model: str
) -> Message:
    """Assemble interleaved text, reasoning, and indexed tool argument deltas."""
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    finished = False
    done = False
    data: list[str] = []

    def consume(raw: str) -> None:
        nonlocal usage, finished, done
        if raw == "[DONE]":
            done = True
            return
        item = json.loads(raw)
        if not isinstance(item, dict) or item.get("error"):
            raise ProviderError("OpenCode Go returned a stream error.")
        if isinstance(item.get("usage"), dict):
            usage = item["usage"]
        for choice in item.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            for key, target in (("content", content), ("reasoning_content", reasoning)):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("Invalid text delta")
                    target.append(value)
            for part in delta.get("tool_calls") or []:
                index = part["index"]
                if type(index) is not int or index < 0:
                    raise ValueError("Invalid tool index")
                call = calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if part.get("id"):
                    call["id"] = part["id"]
                function = part.get("function") or {}
                for key in ("name", "arguments"):
                    if function.get(key) is not None:
                        call["function"][key] += function[key]
            if choice.get("finish_reason") is not None:
                finished = True

    try:
        for line in response.iter_lines():
            if cancel_requested():
                raise ProviderCancelledError()
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                consume("\n".join(data))
                data.clear()
                if done:
                    break
        if data and not done:
            consume("\n".join(data))
        if not done or not finished:
            raise ProviderError("OpenCode Go stream ended before completion.")
        if any(
            not call["id"] or not call["function"]["name"] for call in calls.values()
        ):
            raise ValueError("Incomplete tool call")
        message = OpenAICompatibleResponseMessage.model_validate(
            {
                "role": "assistant",
                "content": "".join(content) or None,
                "reasoning_content": "".join(reasoning) or None,
                "tool_calls": [calls[index] for index in sorted(calls)],
            }
        ).to_message()
        message.usage = parse_token_usage(
            usage, provider_name="opencode-go", model_id=model
        )
        return message
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
        raise ProviderError("OpenCode Go returned an invalid stream payload.") from exc
