"""MCP-only file tools with transport-native result payloads."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL import UnidentifiedImageError
from pydantic import Field

from yoke.agent.tools.base import WorkspaceTool

MAX_VIEW_IMAGE_WIRE_BYTES = 8 * 1024 * 1024
MAX_VIEW_IMAGE_WIRE_OVERHEAD = 64 * 1024
MAX_VIEW_IMAGE_BYTES = (
    (MAX_VIEW_IMAGE_WIRE_BYTES - MAX_VIEW_IMAGE_WIRE_OVERHEAD) * 3 // 4
)
MAX_VIEW_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_VIEW_IMAGE_DECODED_BYTES = 64 * 1024 * 1024

IMAGE_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


class ViewImageError(ValueError):
    """A recoverable image read or validation failure."""


def read_image_bytes(path: Path, *, max_bytes: int = MAX_VIEW_IMAGE_BYTES) -> bytes:
    """Read a regular file without consuming more than the image byte ceiling."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ViewImageError(f"Could not inspect image file: {exc}") from exc
    if size > max_bytes:
        raise ViewImageError(
            f"Image is too large: maximum compressed size is {max_bytes} bytes"
        )
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ViewImageError(f"Could not read image file: {exc}") from exc
    if len(data) > max_bytes:
        raise ViewImageError(
            f"Image is too large: maximum compressed size is {max_bytes} bytes"
        )
    return data


def validate_image_bytes(
    data: bytes,
    *,
    max_pixels: int = MAX_VIEW_IMAGE_PIXELS,
    max_decoded_bytes: int = MAX_VIEW_IMAGE_DECODED_BYTES,
) -> str:
    """Fully decode supported image bytes under explicit resource limits."""
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            if image_format not in IMAGE_MIME_TYPES:
                supported = ", ".join(IMAGE_MIME_TYPES)
                raise ViewImageError(
                    f"Unsupported image format: {image_format or 'unknown'}; "
                    f"supported formats are {supported}"
                )
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ViewImageError("Image dimensions must be positive")
            pixels = width * height
            if pixels > max_pixels:
                raise ViewImageError(
                    f"Image has too many pixels: maximum is {max_pixels}"
                )
            decoded_bytes = pixels * max(1, len(image.getbands()))
            if decoded_bytes > max_decoded_bytes:
                raise ViewImageError(
                    "Image decoded size is too large: maximum is "
                    f"{max_decoded_bytes} bytes"
                )
            image.load()
            return IMAGE_MIME_TYPES[image_format]
    except ViewImageError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ViewImageError(f"Invalid image data: {exc}") from exc


class MCPViewImageTool(WorkspaceTool):
    """Read and validate an image for native MCP image output."""

    name = "view_image"
    description = (
        "View a local PNG, JPEG, GIF, or WebP image. Relative paths resolve from "
        "the configured Yoke MCP root; absolute paths are allowed."
    )

    path: str = Field(
        min_length=1,
        description=(
            "Local path to a PNG, JPEG, GIF, or WebP image. Relative paths "
            "resolve from the configured Yoke MCP root; absolute paths are allowed."
        ),
    )

    def execute(self) -> dict[str, object]:
        """Return validated image data for the MCP adapter's native encoder."""
        try:
            path = self._resolve_path(self.path)
        except FileNotFoundError:
            return self._error(f"Path does not exist: {self.path}")
        except (OSError, ValueError) as exc:
            return self._error(str(exc))
        if not path.is_file():
            return self._error(
                f"Path is not a regular file: {self._display_path(path)}"
            )
        try:
            data = read_image_bytes(path)
            mime_type = validate_image_bytes(data)
        except ViewImageError as exc:
            return self._error(str(exc))
        return self._success(
            path=self._display_path(path),
            mime_type=mime_type,
            data_base64=base64.b64encode(data).decode("ascii"),
            bytes=len(data),
        )
