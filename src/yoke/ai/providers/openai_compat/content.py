"""Helpers for OpenAI-compatible multimodal message serialization."""

from __future__ import annotations

from typing import Any

from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.multimodal import encode_local_image_data_url


def normalize_openai_request_messages(
    messages: list[Message],
) -> list[Message]:
    """Return provider-safe messages for an OpenAI-compatible request."""
    return normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
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
            serialized.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, MessageImageURLContentPart):
            serialized.extend(
                _wrap_image_content(
                    image_url=part.image_url.url,
                    label=None,
                    detail=part.detail,
                )
            )
            continue
        if isinstance(part, MessageLocalImageContentPart):
            serialized.extend(
                _wrap_image_content(
                    image_url=part.data_url or encode_local_image_data_url(part.path),
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


def _local_image_to_data_url(path_value: str) -> str:
    """Read a local image and encode it as a prompt-safe data URL.

    Deprecated: delegates to ``yoke.agent.multimodal.encode_local_image_data_url``.
    Kept for backward compatibility with external callers and tests.
    """
    return encode_local_image_data_url(path_value)
