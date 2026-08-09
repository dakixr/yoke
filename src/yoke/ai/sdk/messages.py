"""Private SDK message input helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.models import MessageContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.ai.sdk.helpers import remote_image_part
from yoke.ai.sdk.helpers import text_part
from yoke.ai.sdk.types import Image


def normalize_image_inputs(
    *,
    images: Sequence[Image | str | Path],
    image_urls: Sequence[str],
) -> tuple[list[Image], list[str]]:
    """Normalize explicit Image values and path shortcuts."""
    normalized_images: list[Image] = []
    for image in images:
        if isinstance(image, Image):
            normalized_images.append(image)
        else:
            normalized_images.append(Image.from_path(image))
    return normalized_images, list(image_urls)


def build_user_message_from_images(
    text: str = "",
    *,
    images: Sequence[Image] = (),
    image_urls: Sequence[str] = (),
) -> Message:
    """Build a multimodal user message from SDK image inputs."""
    if not images and not image_urls:
        return Message.user(text)

    content: list[MessageContentPart] = []
    if text:
        content.append(text_part(text))
    image_index = 1
    for image in images:
        part = image.content
        if isinstance(part, MessageLocalImageContentPart):
            copied = part.model_copy(deep=True)
            if copied.label is None:
                copied.label = f"[Image #{image_index}]"
            content.append(copied)
        else:
            content.append(part)
        image_index += 1
    for image_url in image_urls:
        content.append(remote_image_part(image_url))
        image_index += 1
    return Message.user(content)


def append_text_to_user_message(message: Message, text: str) -> Message:
    """Return a user message with text appended as text content."""
    copied = message.model_copy(deep=True)
    if isinstance(copied.content, list):
        copied.content.append(MessageTextContentPart(text=text))
    elif isinstance(copied.content, str):
        copied.content = f"{copied.content}\n\n{text}"
    else:
        copied.content = text
    return copied
