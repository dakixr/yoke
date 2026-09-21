"""Validation shared by inline ACP attachments and native upload storage."""

from __future__ import annotations

import io
import re
import warnings

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 20
IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def attachment_name(value: str) -> str:
    """Keep a bounded display basename, never a caller-controlled storage path."""
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name)
    return (
        name.encode("utf-8")[:128].decode("utf-8", errors="ignore").strip(" .")
        or "attachment"
    )


def attachment_mime(value: str) -> str:
    """Reject header/control characters and unsupported image media types."""
    if len(value) > 127 or not re.fullmatch(
        r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", value
    ):
        raise ValueError("Invalid attachment MIME type")
    value = value.lower()
    if value.startswith("image/") and value not in IMAGE_MIMES:
        raise ValueError("Unsupported image MIME type")
    return value


def validate_attachment(data: bytes, mime: str) -> None:
    """Validate byte limits and images before any prompt admission."""
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachment exceeds the 20 MiB limit")
    attachment_mime(mime)
    if not mime.startswith("image/"):
        return
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if Image.MIME.get(image.format or "") != mime:
                    raise ValueError("Image bytes do not match the MIME type")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Invalid or unsafe image attachment") from exc
