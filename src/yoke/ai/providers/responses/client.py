"""Small stateless client for OpenAI-compatible Responses APIs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.http_retry import (
    HTTPRetryPolicy,
    post_response,
    response_error,
    run_with_retry,
)
from yoke.ai.providers.openai_compat import (
    normalize_openai_request_messages,
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.events import emit_recovery_event
from yoke.ai.providers.usage import parse_token_usage


def complete_response(
    *,
    client: httpx.Client,
    url: str,
    api_key: str,
    provider_name: str,
    model: str,
    messages: list[Message],
    tools: list[dict[str, object]],
    reasoning_effort: str | None,
    max_output_tokens: int | None,
    max_retries: int,
    retry_backoff_seconds: float,
    max_retry_backoff_seconds: float,
    cancel_requested: Callable[[], bool],
    sleep: Callable[[float], None],
    request_headers: Mapping[str, str] | None = None,
    get_client: Callable[[], httpx.Client] | None = None,
    reset_client: Callable[[], None] | None = None,
) -> Message:
    """Send a non-streaming Responses API request and normalize its output."""
    payload = _request_payload(
        model=model,
        messages=messages,
        tools=tools,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **(request_headers or {}),
    }

    def attempt(number: int) -> Message:
        active_client = get_client() if get_client is not None else client
        response = post_response(active_client, url, json=payload, headers=headers)
        error = response_error(response, prefix=provider_name)
        if error is not None:
            raise error
        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{provider_name} returned an invalid response payload."
            ) from exc
        if not isinstance(result, dict):
            raise ProviderError(
                f"{provider_name} returned an invalid response payload."
            )
        message = _response_message(result, provider_name=provider_name, model=model)
        emit_recovery_event(
            provider_name=provider_name, model_id=model, attempts=number
        )
        return message

    policy = HTTPRetryPolicy(
        provider_name,
        model,
        max_retries,
        retry_backoff_seconds,
        max_retry_backoff_seconds,
        prefix=provider_name,
        check_timeout_cancel=False,
        reset_client=reset_client,
    )
    return run_with_retry(
        attempt,
        lambda error, number: policy.classify(
            error, number, cancel_requested=cancel_requested
        ),
        max_retries=max_retries,
        cancel_requested=cancel_requested,
        sleep=sleep,
    )


def _request_payload(
    *,
    model: str,
    messages: list[Message],
    tools: list[dict[str, object]],
    reasoning_effort: str | None,
    max_output_tokens: int | None,
) -> dict[str, object]:
    instructions, input_items = _convert_messages(messages)
    payload: dict[str, object] = {
        "model": model,
        "input": input_items,
        "store": False,
        "parallel_tool_calls": False,
    }
    if instructions:
        payload["instructions"] = instructions
    if tools:
        payload["tools"] = _convert_tools(tools)
        payload["tool_choice"] = "auto"
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    if reasoning_effort is not None:
        payload["reasoning"] = {
            "effort": reasoning_effort,
            "summary": "auto",
            "context": "all_turns",
        }
    return payload


def _convert_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, object]]]:
    instructions: list[str] = []
    items: list[dict[str, object]] = []
    for message in normalize_openai_request_messages(messages):
        if message.role == "system":
            if text := message.text_content():
                instructions.append(text)
            continue
        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.text_content() or "",
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            if text := message.text_content():
                items.append(_text_message("assistant", text))
            items.extend(
                {
                    "type": "function_call",
                    "call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                }
                for tool_call in message.tool_calls
            )
            continue
        items.append(_convert_text_message(message))
    return "\n\n".join(instructions), items


def _convert_text_message(message: Message) -> dict[str, object]:
    serialized = serialize_message_for_openai(message)
    content = serialized.get("content", "")
    if not isinstance(content, list):
        return _text_message(message.role, str(content or ""))
    parts: list[dict[str, object]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image_url = part.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
            detail = image_url.get("detail") if isinstance(image_url, dict) else None
            parts.append(
                {
                    "type": "input_image",
                    "image_url": url,
                    **({"detail": detail} if detail else {}),
                }
            )
    return {"role": message.role, "content": parts}


def _text_message(role: str, text: str) -> dict[str, object]:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def _convert_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") == "function" and isinstance(function, dict):
            converted.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                    "strict": None,
                }
            )
        else:
            converted.append(tool)
    return converted


def _response_message(
    payload: dict[str, Any], *, provider_name: str, model: str
) -> Message:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    if isinstance(text := part.get("text"), str):
                        text_parts.append(text)
        elif item.get("type") == "function_call":
            tool_calls.append(
                ToolCall(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    function=ToolFunction(
                        name=str(item.get("name") or ""),
                        arguments=str(item.get("arguments") or "{}"),
                    ),
                )
            )
    if not text_parts and not tool_calls:
        raise ProviderError(f"{provider_name} returned no response output.")
    return Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        usage=parse_token_usage(
            payload.get("usage"), provider_name=provider_name, model_id=model
        ),
    )
