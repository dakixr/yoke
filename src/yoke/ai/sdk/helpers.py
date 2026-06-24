"""Helper builders for ergonomic SDK multimodal inputs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.models import MessageContentPart
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart


def text_part(text: str) -> MessageTextContentPart:
    """Create a text content part for a multimodal user message."""
    return MessageTextContentPart(text=text)


def image_part(
    path: str | Path,
    *,
    label: str | None = None,
    detail: str | None = None,
) -> MessageLocalImageContentPart:
    """Create a local image content part from a filesystem path."""
    from yoke.agent.multimodal import encode_local_image_data_url

    resolved = str(Path(path).expanduser().resolve())
    try:
        data_url = encode_local_image_data_url(resolved)
    except OSError:
        data_url = None
    return MessageLocalImageContentPart(
        path=resolved,
        label=label,
        detail=detail,
        data_url=data_url,
    )


def remote_image_part(
    image_url: str,
    *,
    detail: str | None = None,
) -> MessageImageURLContentPart:
    """Create a remote or data-URL image content part."""
    return MessageImageURLContentPart(
        image_url=MessageImageURL(url=image_url),
        detail=detail,
    )


def build_user_message(
    text: str = "",
    *,
    images: Sequence[str | Path] = (),
    image_urls: Sequence[str] = (),
) -> Message:
    """Build a user message with optional local and remote image inputs.

    Images are assigned stable labels in insertion order as `[Image #n]` so the
    text can refer to them naturally.
    """
    if not images and not image_urls:
        return Message.user(text)
    content: list[MessageContentPart] = []
    if text:
        content.append(text_part(text))
    image_index = 1
    for path in images:
        content.append(image_part(path, label=f"[Image #{image_index}]"))
        image_index += 1
    for image_url in image_urls:
        content.append(remote_image_part(image_url))
        image_index += 1
    return Message.user(content)
