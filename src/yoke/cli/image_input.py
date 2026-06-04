"""Helpers for CLI image attachments and multimodal user messages."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from os import PathLike
from os import fspath
from pathlib import Path

from PIL import Image
from PIL import ImageGrab

from yoke.agent.multimodal import IMAGE_EXTENSIONS
from yoke.agent.multimodal import build_image_user_message
from yoke.agent.multimodal import (
    next_image_label_index as next_image_label_index,
)
from yoke.agent.multimodal import (
    resolve_image_path as resolve_image_path,
)
from yoke.agent.models import Message


@dataclass(slots=True, frozen=True)
class ImageAttachment:
    """A pending CLI image attachment."""

    path: Path

    @property
    def label(self) -> str:
        """Return a short human-readable attachment label."""
        return self.path.name


def build_user_message(
    prompt: str,
    *,
    image_paths: Sequence[Path] = (),
    start_index: int = 1,
) -> Message:
    """Build the user message for one CLI turn."""
    return build_image_user_message(
        prompt,
        image_paths=image_paths,
        start_index=start_index,
    )


def paste_image_from_clipboard() -> ImageAttachment | None:
    """Read an image from the clipboard and store it as a temp PNG."""
    grabbed = ImageGrab.grabclipboard()
    if grabbed is None:
        return None
    if isinstance(grabbed, list):
        for item in grabbed:
            if not isinstance(item, str | PathLike):
                continue
            item_path = fspath(item)
            if not isinstance(item_path, str):
                continue
            path = Path(item_path)
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                return ImageAttachment(path=path.resolve())
        return None
    if not isinstance(grabbed, Image.Image):
        return None
    with tempfile.NamedTemporaryFile(
        prefix="yoke-clipboard-",
        suffix=".png",
        delete=False,
    ) as handle:
        grabbed.save(handle.name, format="PNG")
        return ImageAttachment(path=Path(handle.name).resolve())


def attach_standalone_prompt_image_paths(
    prompt: str,
    *,
    root: Path,
) -> tuple[str, list[ImageAttachment]]:
    """Convert standalone image path lines in a prompt into attachments."""
    if not prompt:
        return prompt, []
    lines: list[str] = []
    attachments: list[ImageAttachment] = []
    for line in prompt.splitlines():
        try:
            resolved = resolve_image_path(line, root=root)
        except ValueError:
            lines.append(line)
            continue
        attachment = ImageAttachment(path=resolved)
        attachments.append(attachment)
        indent = line[: len(line) - len(line.lstrip())]
        lines.append(f"{indent}{format_attachment_reference(attachment)}")
    return "\n".join(lines), attachments


def format_attachment_summary(
    attachments: Sequence[ImageAttachment],
) -> str | None:
    """Return a compact toolbar summary for pending image attachments."""
    if not attachments:
        return None
    if len(attachments) == 1:
        return f"1 image ({attachments[0].label})"
    return f"{len(attachments)} images"


def format_attachment_lines(
    attachments: Sequence[ImageAttachment],
) -> list[str]:
    """Return toolbar lines for pending image attachments."""
    lines: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        lines.append(f" image {index}: {attachment.label} ")
    return lines


def format_attachment_reference(attachment: ImageAttachment) -> str:
    """Return the inline prompt reference for an image attachment."""
    return f"[{attachment.label}]"
