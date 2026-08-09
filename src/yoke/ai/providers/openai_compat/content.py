"""Helpers for OpenAI-compatible multimodal message serialization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yoke.agent.image_data import (
    local_image_to_data_url as _local_image_to_data_url,
)
from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart


def normalize_openai_request_messages(
    messages: list[Message],
) -> list[Message]:
    """Return provider-safe messages for an OpenAI-compatible request."""
    return normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
        drop_orphan_tool_results=True,
    )


DEFAULT_IMAGE_DETAIL = "high"


def serialize_message_for_openai(message: Message) -> dict[str, object]:
    """Serialize one message to the OpenAI chat-completions shape."""
    payload: dict[str, object] = {"role": message.role}
    if message.content is not None or message.role == "assistant":
        serialized_content = _serialize_content(message)
        if serialized_content is None and message.role == "assistant":
            serialized_content = ""
        payload["content"] = serialized_content
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            tool_call.model_dump() for tool_call in message.tool_calls
        ]
    if message.reasoning_content is not None:
        payload["reasoning_content"] = message.reasoning_content
    return payload


def _serialize_content(message: Message) -> object:
    content = message.content
    if not isinstance(content, list):
        return content
    serialized: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, MessageTextContentPart):
            text_payload: dict[str, Any] = {"type": "text", "text": part.text}
            if part.cache_control is not None:
                text_payload["cache_control"] = part.cache_control
            serialized.append(text_payload)
            continue
        if isinstance(part, MessageImageURLContentPart):
            serialized.extend(
                _wrap_image_content(
                    image_url=part.image_url.url,
                    label=part.label,
                    detail=part.detail,
                )
            )
            continue
        if isinstance(part, MessageLocalImageContentPart):
            try:
                image_url = _local_image_to_data_url(part.path)
            except OSError:
                serialized.append(_missing_local_image_content(part))
                continue
            serialized.extend(
                _wrap_image_content(
                    image_url=image_url,
                    label=part.display_label,
                    detail=part.detail,
                )
            )
    return serialized


def _wrap_image_content(
    *,
    image_url: str,
    label: str | None,
    detail: str | None,
) -> list[dict[str, Any]]:
    opening = "<image>" if label is None else f"<image name={label}>"
    image_payload: dict[str, Any] = {
        "type": "image_url",
        "image_url": {
            "url": image_url,
            "detail": detail or DEFAULT_IMAGE_DETAIL,
        },
    }
    return [
        {"type": "text", "text": opening},
        image_payload,
        {"type": "text", "text": "</image>"},
    ]


def _missing_local_image_content(
    part: MessageLocalImageContentPart,
) -> dict[str, str]:
    path = Path(part.path).expanduser().resolve()
    return {
        "type": "text",
        "text": (
            f"[Image unavailable: {part.display_label} was attached from "
            f"{path}, but that local file no longer exists.]"
        ),
    }
