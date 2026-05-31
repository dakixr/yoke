"""Helpers for OpenAI-compatible multimodal message serialization."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from PIL import Image

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
    )


MAX_IMAGE_DIMENSION = 2048
DEFAULT_IMAGE_DETAIL = "high"
_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


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
                    image_url=_local_image_to_data_url(part.path),
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
    """Read a local image and encode it as a prompt-safe data URL."""
    path = Path(path_value).expanduser().resolve()
    original_bytes = path.read_bytes()
    with Image.open(io.BytesIO(original_bytes)) as image:
        image.load()
        image_format = (image.format or "PNG").upper()
        preserve_original = image_format in _MIME_TYPES
        should_resize = (
            image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION
        )
        if not should_resize and preserve_original:
            encoded_bytes = original_bytes
            mime_type = _MIME_TYPES[image_format]
        else:
            encoded_bytes, mime_type = _encode_processed_image(
                image, image_format=image_format
            )
    encoded = base64.b64encode(encoded_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _encode_processed_image(
    image: Image.Image, *, image_format: str
) -> tuple[bytes, str]:
    output = io.BytesIO()
    resized = image.copy()
    resized.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    if image_format == "JPEG":
        if resized.mode not in {"RGB", "L"}:
            resized = resized.convert("RGB")
        resized.save(output, format="JPEG", quality=85)
        return output.getvalue(), "image/jpeg"
    if image_format == "WEBP":
        resized.save(output, format="WEBP")
        return output.getvalue(), "image/webp"
    resized.save(output, format="PNG")
    return output.getvalue(), "image/png"
