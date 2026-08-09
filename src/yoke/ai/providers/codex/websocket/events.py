"""Event parsing and error mapping for Codex WebSockets."""

from __future__ import annotations

from typing import Any

from websockets.exceptions import InvalidStatus

from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import (
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.codex.subscription import (
    merge_completed_response,
    message_phase_from_completed_response,
    normalize_message_phase,
)
from yoke.ai.providers.codex.websocket.config import (
    CodexPreviousResponseNotFoundError,
    CodexWebSocketParseState,
)
from yoke.ai.providers.usage import parse_token_usage


def handle_websocket_event(
    event: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    event_type = event.get("type")
    if isinstance(event.get("usage"), dict):
        state.usage_payload = event.get("usage")
    if event_type == "response.created":
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            response_id = response_payload.get("id")
            if isinstance(response_id, str) and response_id:
                state.response_id = response_id
    if event_type in {"error", "response.failed"}:
        raise map_websocket_error_event(event)
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            state.text_parts.append(delta)
    elif event_type == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or event.get("output_index") or "")
        if item_id:
            item = state.function_calls.setdefault(item_id, {})
            item["arguments"] = item.get("arguments", "") + str(
                event.get("delta") or ""
            )
    elif event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict):
            state.output_items.append(item)
            handle_websocket_output_item(item, state)
    elif event_type in {"response.completed", "response.done"}:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            state.completed_payload = response_payload
            completed_output = response_payload.get("output")
            if not state.output_items and isinstance(completed_output, list):
                state.output_items.extend(
                    item for item in completed_output if isinstance(item, dict)
                )
            response_id = response_payload.get("id")
            if isinstance(response_id, str) and response_id:
                state.response_id = response_id
            state.usage_payload = response_payload.get("usage") or state.usage_payload


def handle_websocket_output_item(
    item: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    if item.get("type") == "function_call":
        item_id = str(
            item.get("id") or item.get("call_id") or len(state.function_calls)
        )
        stored = state.function_calls.setdefault(item_id, {})
        stored["call_id"] = str(item.get("call_id") or item_id)
        stored["name"] = str(item.get("name") or "")
        stored["arguments"] = str(
            item.get("arguments") or stored.get("arguments") or "{}"
        )
        return
    if item.get("type") != "message":
        return
    phase = normalize_message_phase(item.get("phase"))
    if phase == "final_answer" or (phase == "commentary" and state.phase is None):
        state.phase = phase
    for content in item.get("content") or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") in {"output_text", "text"}:
            text = content.get("text")
            if isinstance(text, str):
                state.snapshot_text_parts.append(text)


def build_message_from_websocket_state(
    state: CodexWebSocketParseState,
    *,
    provider_name: str,
    model_id: str,
) -> Message:
    if state.completed_payload is not None:
        merge_completed_response(
            state.completed_payload,
            state.text_parts if state.text_parts else state.snapshot_text_parts,
            state.function_calls,
        )
        state.usage_payload = (
            state.completed_payload.get("usage") or state.usage_payload
        )
    phase = (
        message_phase_from_completed_response(state.completed_payload) or state.phase
    )
    text_parts = state.text_parts or state.snapshot_text_parts
    tool_calls = [
        ToolCall(
            id=item.get("call_id") or item_id,
            function=ToolFunction(
                name=item.get("name") or "",
                arguments=item.get("arguments") or "{}",
            ),
        )
        for item_id, item in state.function_calls.items()
        if item.get("name")
    ]
    return Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        phase=phase,
        usage=parse_token_usage(
            state.usage_payload,
            provider_name=provider_name,
            model_id=model_id,
        ),
    )


def map_websocket_error_event(event: dict[str, Any]) -> ProviderError:
    error_payload = event.get("error") if isinstance(event, dict) else None
    response_payload = event.get("response") if isinstance(event, dict) else None
    if not isinstance(error_payload, dict) and isinstance(response_payload, dict):
        nested_error_payload = response_payload.get("error")
        if isinstance(nested_error_payload, dict):
            error_payload = nested_error_payload
    error_type = ""
    error_code = ""
    error_message = ""
    if isinstance(error_payload, dict):
        error_type = str(error_payload.get("type") or "").lower()
        error_code = str(error_payload.get("code") or "").lower()
        error_message = str(error_payload.get("message") or "").lower()
    status_code = event.get("status") or event.get("status_code")
    if not status_code and isinstance(response_payload, dict):
        status_code = response_payload.get("status") or response_payload.get(
            "status_code"
        )
    haystack = f"{error_type} {error_code} {error_message}"
    if "websocket_connection_limit_reached" in haystack:
        return ProviderServerError(
            f"Codex WebSocket connection limit reached: {event}",
            status_code=503,
        )
    previous_response_problem = (
        "previous_response_not_found" in haystack
        or "codex_previous_response_stale" in haystack
        or "previous_response_stale" in haystack
        or "previous_response_id" in haystack
        or "previous response anchor expired" in haystack
        or ("previous" in haystack and "not found" in haystack)
        or ("prev" in haystack and "msg" in haystack)
    )
    if previous_response_problem:
        return CodexPreviousResponseNotFoundError(
            f"Codex WebSocket previous response was not found: {event}",
            status_code=status_code if isinstance(status_code, int) else None,
        )
    if "rate_limit" in haystack or status_code == 429:
        return ProviderRateLimitError(f"Codex WebSocket rate limited: {event}")
    if status_code in {500, 502, 503, 504} or any(
        marker in haystack
        for marker in (
            "server_error",
            "service_unavailable",
            "internal_error",
            "overloaded",
            "timeout",
            "bad_gateway",
            "gateway_timeout",
        )
    ):
        return ProviderServerError(
            f"Codex WebSocket stream failed: {event}",
            status_code=503,
        )
    return ProviderError(
        f"Codex WebSocket stream failed: {event}",
        status_code=status_code if isinstance(status_code, int) else None,
    )


def map_websocket_status_error(exc: InvalidStatus) -> ProviderError:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(
        response, "status", None
    )
    message = f"Codex WebSocket handshake failed: {exc}"
    if status_code == 429:
        return ProviderRateLimitError(message)
    if status_code in {500, 502, 503, 504}:
        return ProviderServerError(message, status_code=status_code)
    return ProviderError(
        message, status_code=status_code if isinstance(status_code, int) else None
    )
