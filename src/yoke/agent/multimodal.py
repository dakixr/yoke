"""Shared helpers for multimodal image attachments in conversation context."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlparse

from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
}
_IMAGE_LABEL_PATTERN = re.compile(r"^\[Image #(\d+)\]$")


def format_image_label(index: int) -> str:
    """Return the stable model-facing label for an attached image."""
    if index < 1:
        raise ValueError("Image labels are 1-based")
    return f"[Image #{index}]"


def next_image_label_index(messages: Sequence[Message]) -> int:
    """Return the next available image label index.

    Scans existing conversation messages for stable `[Image #N]` labels.
    """
    highest = 0
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, MessageLocalImageContentPart):
                continue
            match = _IMAGE_LABEL_PATTERN.match(part.display_label)
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
    return highest + 1


def build_image_user_message(
    prompt: str,
    *,
    image_paths: Sequence[Path] = (),
    start_index: int = 1,
) -> Message:
    """Build a user message containing text plus local image parts."""
    if not image_paths:
        return Message.user(prompt)
    content: list[MessageContentPart] = []
    if prompt:
        content.append(MessageTextContentPart(text=prompt))
    for index, path in enumerate(image_paths, start=start_index):
        content.append(
            MessageLocalImageContentPart(
                path=str(path.resolve()),
                label=format_image_label(index),
            )
        )
    return Message.user(content)


def omit_image_inputs_for_text_model(
    messages: Sequence[Message],
) -> list[Message]:
    """Return provider-bound messages with images replaced by text notes."""
    return [_omit_image_inputs_from_message(message) for message in messages]


def messages_for_provider_capabilities(
    messages: Sequence[Message], provider: object
) -> list[Message]:
    """Adapt provider-bound messages to the active provider capabilities."""
    if _provider_supports_image_inputs(provider) is False:
        return omit_image_inputs_for_text_model(messages)
    return [message.model_copy(deep=True) for message in messages]


def _provider_supports_image_inputs(provider: object) -> bool | None:
    current_model_info = getattr(provider, "current_model_info", None)
    if callable(current_model_info):
        model_info = current_model_info()
        model_support = getattr(model_info, "supports_image_inputs", None)
        if isinstance(model_support, bool):
            return model_support
    provider_support = getattr(provider, "supports_image_inputs", None)
    return provider_support if isinstance(provider_support, bool) else None


def _omit_image_inputs_from_message(message: Message) -> Message:
    if not isinstance(message.content, list):
        return message.model_copy(deep=True)
    content: list[MessageContentPart] = []
    for part in message.content:
        if isinstance(part, MessageTextContentPart):
            content.append(part.model_copy(deep=True))
            continue
        if isinstance(part, MessageLocalImageContentPart):
            content.append(
                MessageTextContentPart(
                    text=_image_omission_placeholder(part.display_label)
                )
            )
            continue
        if isinstance(part, MessageImageURLContentPart):
            content.append(
                MessageTextContentPart(text=_image_omission_placeholder("[Image]"))
            )
            continue
        content.append(part.model_copy(deep=True))
    updated = message.model_copy(deep=True)
    updated.content = content
    return updated


def _image_omission_placeholder(label: str) -> str:
    return (
        f"[Image omitted: {label} was attached in the original "
        "conversation, but the active model does not support image inputs.]"
    )


def resolve_image_path(raw: str, *, root: Path) -> Path:
    """Resolve and validate a local image path."""
    candidate = raw.strip().strip("\"'")
    if candidate.startswith("file://"):
        parsed = urlparse(candidate)
        candidate = unquote(parsed.path)
    path = Path(candidate)
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Image file not found: {path}")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(
            "Unsupported image format. Supported extensions: "
            + ", ".join(sorted(IMAGE_EXTENSIONS))
        )
    return path
