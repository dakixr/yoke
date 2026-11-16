"""Storage-only normalization of provider usage and existing image carriers."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from yoke.agent.image_data import image_bytes_to_data_url


def strip_usage_raw(value: Any) -> None:
    """Drop raw usage on messages, including retained handoff messages."""
    if isinstance(value, list):
        for item in value:
            strip_usage_raw(item)
    elif isinstance(value, dict):
        if value.get("role") in {"assistant", "user", "tool", "system"}:
            usage = value.get("usage")
            if isinstance(usage, dict):
                usage.pop("raw", None)
        for child in value.values():
            strip_usage_raw(child)


@dataclass
class ImageTransform:
    """Track approved image changes and leave unsupported data inline."""

    reencoded: int = 0
    failures: int = 0
    failed_urls: set[str] = field(default_factory=set)

    def apply(self, value: Any) -> None:
        """Visit only image_url and local_image carriers, including metadata."""
        if isinstance(value, list):
            for item in value:
                self.apply(item)
        elif isinstance(value, dict):
            if value.get("type") == "image_url" and isinstance(
                value.get("image_url"), dict
            ):
                self._replace(value["image_url"], "url")
            elif value.get("type") == "local_image":
                self._replace(value, "data_url")
            for child in value.values():
                self.apply(child)

    def _replace(self, carrier: dict[str, Any], key: str) -> None:
        url = carrier.get(key)
        if not isinstance(url, str) or not url.startswith("data:"):
            return

        prefix, separator, payload = url.partition(",")
        if not separator or not prefix.endswith(";base64"):
            self.failures += 1
            self.failed_urls.add(url)
            return
        from PIL import Image

        try:
            raw = base64.b64decode(payload, validate=True)
            if base64.b64encode(raw).decode("ascii") != payload:
                self.failures += 1
                self.failed_urls.add(url)
                return
            candidate = image_bytes_to_data_url(raw)
        except (ValueError, OSError, binascii.Error, Image.DecompressionBombError):
            self.failures += 1
            self.failed_urls.add(url)
            return
        # Preserve unusual but valid MIME prefixes if the bytes did not change.
        if candidate.partition(",")[2] != payload:
            carrier[key] = candidate
            self.reencoded += 1


def transform_entry(raw: bytes) -> tuple[bytes, ImageTransform]:
    """Normalize a legacy JSON blob without materializing new model defaults."""
    value = json.loads(raw)
    strip_usage_raw(value)
    images = ImageTransform()
    images.apply(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(), images
