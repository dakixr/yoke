"""Upload the terminal's existing image projection without changing its bytes."""

from __future__ import annotations

import base64
from pathlib import Path

from yoke.agent.models import Message
from yoke.cli.backend.connection import SessionConnection
from yoke.http.models.upload import UploadResponse


def upload_message_images(
    connection: SessionConnection, message: Message | None
) -> list[dict[str, str]]:
    if message is None or not isinstance(message.content, list):
        return []
    attachments: list[dict[str, str]] = []
    for part in message.content:
        if part.type != "image_url":
            continue
        uri = part.image_url.url
        if not uri.startswith("data:"):
            raise ValueError("Terminal images must contain their local image data.")
        header, encoded = uri.split(",", 1)
        mime = header.removeprefix("data:").split(";", 1)[0]
        name = Path(part.label or f"image-{len(attachments) + 1}.png").name
        uploaded = UploadResponse.model_validate(
            connection.request(
                "POST",
                "upload",
                params={"sessionID": connection.session_id},
                files={"file": (name, base64.b64decode(encoded, validate=True), mime)},
            )
        ).data
        attachments.append(
            {
                "type": "file",
                "uri": uploaded.uri,
                "name": uploaded.name,
                "mime": uploaded.mime,
                "imageProjection": "cli",
            }
        )
    return attachments
