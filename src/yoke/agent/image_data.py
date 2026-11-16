"""Durable image snapshots within provider dimension and pixel limits."""

from __future__ import annotations

import base64
import io
import math
from pathlib import Path

MAX_IMAGE_DIMENSION = 1568
MAX_IMAGE_PIXELS = 1_150_000
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def local_image_to_data_url(path_value: str | Path) -> str:
    """Read a local image and encode it as a prompt-safe data URL."""
    return image_bytes_to_data_url(Path(path_value).expanduser().resolve().read_bytes())


def image_bytes_to_data_url(original_bytes: bytes) -> str:
    """Fit both provider limits, using JPEG quality 90 or PNG for transparency.

    Keep a supported original if it already fits and is no larger than the
    candidate. Never keep an oversized original, even if it compresses well.
    """
    from PIL import Image

    with Image.open(io.BytesIO(original_bytes)) as image:
        image.load()
        image_format = (image.format or "PNG").upper()
        scale = min(
            1.0,
            MAX_IMAGE_DIMENSION / max(image.size),
            math.sqrt(MAX_IMAGE_PIXELS / (image.width * image.height)),
        )
        resized = image.convert("RGBA")
        if scale < 1:
            resized = resized.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        alpha = resized.getchannel("A").getextrema() != (255, 255)
        output = io.BytesIO()
        if alpha:
            resized.save(output, format="PNG")
            mime = "image/png"
        else:
            resized.convert("RGB").save(output, format="JPEG", quality=90)
            mime = "image/jpeg"
        encoded_bytes = output.getvalue()
        if (
            scale == 1
            and image_format in MIME_TYPES
            and len(original_bytes) <= len(encoded_bytes)
        ):
            encoded_bytes, mime = original_bytes, MIME_TYPES[image_format]
    return f"data:{mime};base64,{base64.b64encode(encoded_bytes).decode('ascii')}"
