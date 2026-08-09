"""Streaming response assembly for Z.AI."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import ProviderCancelledError, ProviderError
from yoke.ai.providers.usage import parse_token_usage
from yoke.ai.providers.zai.models import PROVIDER_NAME
from yoke.ai.providers.zai.models import ZAIConfig


class ZAIStreamingMixin:
    config: ZAIConfig

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
