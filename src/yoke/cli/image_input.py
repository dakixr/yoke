"""Helpers for CLI image attachments and multimodal user messages."""

from __future__ import annotations

import tempfile
import sys
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
        embed_local_images=True,
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


def paste_text_from_clipboard() -> str:
    """Read text from the OS clipboard."""
    if sys.platform == "win32":
        return _paste_text_from_windows_clipboard()
    return ""


def _paste_text_from_windows_clipboard() -> str:
    """Read Unicode text from the Windows clipboard."""
    import ctypes
    from ctypes import wintypes

    cf_unicode_text = 13
    windll = getattr(ctypes, "windll")
    user32 = windll.user32
    kernel32 = windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(cf_unicode_text):
            return ""
        handle = user32.GetClipboardData(cf_unicode_text)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


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
