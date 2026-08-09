"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import httpx

from yoke.agent.models import Message, MessagePhase, ToolCall, ToolFunction
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.usage import parse_token_usage

from .catalog import X_CODEX_TURN_STATE_HEADER


def consume_sse_response(
    response: httpx.Response,
    *,
    provider_name: str | None = None,
    model_id: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    turn_state_updated: Callable[[str], None] | None = None,
) -> Message:
    text_parts: list[str] = []
    function_calls: dict[str, dict[str, str]] = {}
    completed_payload: dict[str, Any] | None = None
    usage_payload: object | None = None
    event_lines: list[str] = []
    finished = threading.Event()
    response_closed = threading.Event()

    def close_on_cancel() -> None:
        if cancel_requested is None:
            return
        while not finished.wait(0.05):
            if cancel_requested():
                response_closed.set()
                response.close()
                return

    watcher = threading.Thread(target=close_on_cancel, daemon=True)
    watcher.start()
    try:
        for line in response.iter_lines():
            if cancel_requested is not None and cancel_requested():
                raise ProviderCancelledError()
            if line == "":
                completed_payload, usage_payload = handle_sse_event(
                    event_lines,
                    text_parts,
                    function_calls,
                    completed_payload,
                    usage_payload,
                    turn_state_updated=turn_state_updated,
                )
                event_lines = []
                continue
            event_lines.append(line)
    except httpx.HTTPError as exc:
        if cancel_requested is not None and cancel_requested():
            raise ProviderCancelledError() from exc
        raise
    finally:
        finished.set()
    if response_closed.is_set() and cancel_requested is not None and cancel_requested():
        raise ProviderCancelledError()
    if event_lines:
        completed_payload, usage_payload = handle_sse_event(
            event_lines,
            text_parts,
            function_calls,
            completed_payload,
            usage_payload,
            turn_state_updated=turn_state_updated,
        )
    if completed_payload is not None:
        merge_completed_response(completed_payload, text_parts, function_calls)
        usage_payload = completed_payload.get("usage") or usage_payload
    phase = message_phase_from_completed_response(completed_payload)
    tool_calls = [
        ToolCall(
            id=item.get("call_id") or item_id,
            function=ToolFunction(
                name=item.get("name") or "",
                arguments=item.get("arguments") or "{}",
            ),
        )
        for item_id, item in function_calls.items()
        if item.get("name")
    ]
    return Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        phase=phase,
        usage=parse_token_usage(
            usage_payload,
            provider_name=provider_name,
            model_id=model_id,
        ),
    )


def consume_hosted_image_sse_response(response: httpx.Response) -> str:
    latest_image: str | None = None
    event_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            latest_image = handle_hosted_image_sse_event(event_lines, latest_image)
            event_lines = []
            continue
        event_lines.append(line)
    if event_lines:
        latest_image = handle_hosted_image_sse_event(event_lines, latest_image)
    if not latest_image:
        raise ProviderError("Codex image generation did not return image data.")
    return latest_image


def handle_hosted_image_sse_event(
    lines: list[str], latest_image: str | None
) -> str | None:
    data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return latest_image
    raw_data = "\n".join(data_lines)
    if raw_data == "[DONE]":
        return latest_image
    try:
        event = json.loads(raw_data)
    except json.JSONDecodeError:
        return latest_image
    event_type = event.get("type")
    if event_type in {"error", "response.failed"}:
        raise ProviderError(f"Codex image generation failed: {event}")
    if event_type == "response.image_generation_call.partial_image":
        partial_image = event.get("partial_image_b64")
        if isinstance(partial_image, str) and partial_image:
            return partial_image
    if event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "image_generation_call":
            result = item.get("result")
            if isinstance(result, str) and result:
                return result
    if event_type in {"response.completed", "response.done"}:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            for item in response_payload.get("output") or []:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "image_generation_call"
                ):
                    result = item.get("result")
                    if isinstance(result, str) and result:
                        return result
    return latest_image


def handle_sse_event(
    lines: list[str],
    text_parts: list[str],
    function_calls: dict[str, dict[str, str]],
    completed_payload: dict[str, Any] | None,
    usage_payload: object | None,
    *,
    turn_state_updated: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any] | None, object | None]:
    data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return completed_payload, usage_payload
    raw_data = "\n".join(data_lines)
    if raw_data == "[DONE]":
        return completed_payload, usage_payload
    try:
        event = json.loads(raw_data)
    except json.JSONDecodeError:
        return completed_payload, usage_payload
    event_type = event.get("type")
    capture_turn_state(event, turn_state_updated)
    if isinstance(event.get("usage"), dict):
        usage_payload = event.get("usage")
    if event_type in {"error", "response.failed"}:
        error_payload = event.get("error") if isinstance(event, dict) else None
        error_type = ""
        error_code = ""
        error_message = ""
        if isinstance(error_payload, dict):
            error_type = str(error_payload.get("type") or "").lower()
            error_code = str(error_payload.get("code") or "").lower()
            error_message = str(error_payload.get("message") or "").lower()
        haystack = f"{error_type} {error_code} {error_message}"
        transient_markers = (
            "server_error",
            "service_unavailable",
            "internal_error",
            "overloaded",
            "server_is_overloaded",
            "timeout",
            "bad_gateway",
            "gateway_timeout",
            "temporarily unavailable",
            "currently overloaded",
            "try again later",
        )
        if any(marker in haystack for marker in transient_markers):
            raise ProviderServerError(
                f"Codex stream failed: {event}",
                status_code=503,
            )
        if "rate_limit" in haystack:
            raise ProviderRateLimitError(f"Codex stream rate limited: {event}")
        raise ProviderError(f"Codex stream failed: {event}")
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            text_parts.append(delta)
    elif event_type == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or event.get("output_index") or "")
        if item_id:
            item = function_calls.setdefault(item_id, {})
            item["arguments"] = item.get("arguments", "") + str(
                event.get("delta") or ""
            )
    elif event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            item_id = str(item.get("id") or item.get("call_id") or len(function_calls))
            stored = function_calls.setdefault(item_id, {})
            stored["call_id"] = str(item.get("call_id") or item_id)
            stored["name"] = str(item.get("name") or "")
            stored["arguments"] = str(
                item.get("arguments") or stored.get("arguments") or "{}"
            )
    elif event_type in {"response.completed", "response.done"}:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            usage_payload = response_payload.get("usage") or usage_payload
            return response_payload, usage_payload
    return completed_payload, usage_payload


def capture_turn_state(
    event: dict[str, Any], callback: Callable[[str], None] | None
) -> None:
    # Codex HTTP/SSE and WebSockets both surface x-codex-turn-state through a
    # response.metadata event. Replaying it keeps retries/reconnects sticky to
    # the warm backend without tying affinity to one physical connection.
    if callback is None or event.get("type") != "response.metadata":
        return
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return
    for name, value in headers.items():
        if name.lower() != X_CODEX_TURN_STATE_HEADER:
            continue
        if isinstance(value, str) and value.strip():
            callback(value.strip())
        return


def merge_completed_response(
    payload: dict[str, Any],
    text_parts: list[str],
    function_calls: dict[str, dict[str, str]],
) -> None:
    output = payload.get("output")
    if not isinstance(output, list):
        return
    if text_parts:
        existing_text = "".join(text_parts)
    else:
        existing_text = ""
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            content_items = item.get("content")
            if not isinstance(content_items, list):
                content_items = []
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text not in existing_text:
                        text_parts.append(text)
        if item.get("type") == "function_call":
            item_id = str(item.get("id") or item.get("call_id") or index)
            stored = function_calls.setdefault(item_id, {})
            stored["call_id"] = str(item.get("call_id") or item_id)
            stored["name"] = str(item.get("name") or "")
            stored["arguments"] = str(
                item.get("arguments") or stored.get("arguments") or "{}"
            )


def message_phase_from_completed_response(
    payload: dict[str, Any] | None,
) -> MessagePhase | None:
    if payload is None:
        return None
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    seen_commentary = False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        phase = normalize_message_phase(item.get("phase"))
        if phase == "final_answer":
            return phase
        if phase == "commentary":
            seen_commentary = True
    return "commentary" if seen_commentary else None


def normalize_message_phase(value: object) -> MessagePhase | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"commentary", "preamble"}:
        return "commentary"
    if normalized in {"final_answer", "final"}:
        return "final_answer"
    return None
