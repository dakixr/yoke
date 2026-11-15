"""Parse Codex SSE text, tools, image results and routing metadata."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
)
from yoke.ai.providers.cancellation import cancellation_watcher
from yoke.ai.providers.codex.events import (
    CodexResponseState,
    build_message,
    reduce_event,
)

from .catalog import X_CODEX_TURN_STATE_HEADER


def consume_sse_response(
    response: httpx.Response,
    *,
    provider_name: str | None = None,
    model_id: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    turn_state_updated: Callable[[str], None] | None = None,
) -> Message:
    state = CodexResponseState(text_parts=[], function_calls={}, websocket=False)
    event_lines: list[str] = []

    def consume() -> None:
        raw = "\n".join(
            line[5:].strip() for line in event_lines if line.startswith("data:")
        )
        if not raw or raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        capture_turn_state(event, turn_state_updated)
        reduce_event(event, state)

    with cancellation_watcher(cancel_requested, response.close):
        try:
            for line in response.iter_lines():
                if cancel_requested is not None and cancel_requested():
                    raise ProviderCancelledError()
                if line == "":
                    consume()
                    event_lines = []
                else:
                    event_lines.append(line)
        except httpx.HTTPError as exc:
            if cancel_requested is not None and cancel_requested():
                raise ProviderCancelledError() from exc
            raise
    if event_lines:
        consume()
    return build_message(state, provider_name=provider_name, model_id=model_id)


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
