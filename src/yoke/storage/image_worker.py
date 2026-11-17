"""Bounded image decoding in a disposable process, without database access."""

from __future__ import annotations

import io
import subprocess
import sys
import warnings

from yoke.agent.image_data import image_bytes_to_data_url

MAX_COMPACTION_IMAGE_PIXELS = 20_000_000
IMAGE_TRANSFORM_TIMEOUT_SECONDS = 5


def reencode_image(
    raw: bytes, *, timeout: float = IMAGE_TRANSFORM_TIMEOUT_SECONDS
) -> str:
    """Contain native decoder crashes and kill hung decoders after five seconds."""
    result = subprocess.run(  # noqa: S603 - fixed module in this interpreter
        [sys.executable, "-P", "-m", "yoke.storage.image_worker"],
        input=raw,
        capture_output=True,
        timeout=min(timeout, IMAGE_TRANSFORM_TIMEOUT_SECONDS),
        check=True,
    )
    return result.stdout.decode("ascii")


def main() -> None:
    """Decode only after checking the image header's dimensions."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_COMPACTION_IMAGE_PIXELS
    raw = sys.stdin.buffer.read()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_COMPACTION_IMAGE_PIXELS:
                raise ValueError("Compaction image exceeds pixel limit")
        sys.stdout.write(image_bytes_to_data_url(raw))


if __name__ == "__main__":
    main()
