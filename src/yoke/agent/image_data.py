"""Helpers for durable local image snapshots."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

MAX_IMAGE_DIMENSION = 2048
MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


def local_image_to_data_url(path_value: str | Path) -> str:
    """Read a local image and encode it as a prompt-safe data URL."""
    path = Path(path_value).expanduser().resolve()
    return image_bytes_to_data_url(path.read_bytes())


def image_bytes_to_data_url(original_bytes: bytes) -> str:
    """Encode image bytes as a data URL, resizing oversized images."""
    from PIL import Image

    with Image.open(io.BytesIO(original_bytes)) as image:
        image.load()
        image_format = (image.format or "PNG").upper()
        preserve_original = image_format in MIME_TYPES
        should_resize = (
            image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION
        )
        if not should_resize and preserve_original:
            encoded_bytes = original_bytes
            mime_type = MIME_TYPES[image_format]
        else:
            encoded_bytes, mime_type = _encode_processed_image(
                image,
                image_format=image_format,
            )
    encoded = base64.b64encode(encoded_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _encode_processed_image(
    image: Image.Image,
    *,
    image_format: str,
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
