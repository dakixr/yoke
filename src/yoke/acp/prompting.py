"""Decode ACP inline input without dereferencing client-supplied resource URIs."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from acp import RequestError

from yoke.agent.attachments import IMAGE_MIMES
from yoke.agent.attachments import MAX_ATTACHMENT_BYTES
from yoke.agent.attachments import MAX_ATTACHMENTS
from yoke.agent.attachments import attachment_mime
from yoke.agent.attachments import attachment_name
from yoke.agent.attachments import validate_attachment

# Leave room for JSON escaping, metadata and the RPC envelope below ACP's 50 MiB.
MAX_INLINE_BYTES = 32 * 1024 * 1024
MAX_PROMPT_JSON_BYTES = 48 * 1024 * 1024


@dataclass(frozen=True)
class InlineAttachment:
    name: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class DecodedPrompt:
    text: str
    attachments: list[InlineAttachment]
    continuation: bool = False


def decode_prompt(
    blocks: list[Any], meta: dict[str, Any], *, images: bool
) -> DecodedPrompt:
    """Validate the entire input before uploads or model-visible admission."""
    try:
        marker = meta.get("yokeContinuation", False)
        if not isinstance(marker, bool):
            raise ValueError("yokeContinuation must be a boolean")
        if marker:
            if blocks:
                raise ValueError("Continuation requires prompt:[]")
            return DecodedPrompt("", [], True)
        values = [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(block, "model_dump")
            else block
            for block in blocks
        ]
        if len(json.dumps(values, ensure_ascii=True).encode()) > MAX_PROMPT_JSON_BYTES:
            raise ValueError("ACP prompt exceeds the inline JSON limit")
        text: list[str] = []
        attachments: list[InlineAttachment] = []
        total = 0
        for block in values:
            if not isinstance(block, dict):
                raise ValueError("Invalid ACP content block")
            kind = block.get("type")
            if kind == "text":
                value = block.get("text")
                if not isinstance(value, str):
                    raise ValueError("Invalid text block")
                text.append(value)
                continue
            if kind not in {"image", "resource"}:
                raise ValueError("Unsupported ACP content type")
            if len(attachments) >= MAX_ATTACHMENTS:
                raise ValueError("Prompt accepts at most 20 attachments")
            resource = block if kind == "image" else block.get("resource")
            if not isinstance(resource, dict):
                raise ValueError("Invalid embedded resource")
            mime = attachment_mime(resource.get("mimeType", "application/octet-stream"))
            if kind == "image" and not mime.startswith("image/"):
                raise ValueError("Image block requires an image MIME type")
            if mime.startswith("image/") and not images:
                raise ValueError("Selected model does not support image inputs")
            if kind == "resource" and "text" in resource:
                if "blob" in resource or not isinstance(resource["text"], str):
                    raise ValueError("Ambiguous or invalid embedded resource")
                data = resource["text"].encode("utf-8")
            else:
                encoded = resource.get("data" if kind == "image" else "blob")
                if not isinstance(encoded, str) or len(encoded) > 4 * (
                    (MAX_ATTACHMENT_BYTES + 2) // 3
                ):
                    raise ValueError("Missing or oversized attachment base64")
                data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if total > MAX_INLINE_BYTES:
                raise ValueError("ACP attachments exceed the aggregate limit")
            validate_attachment(data, mime)
            metadata = block.get("_meta") or {}
            name = metadata.get(
                "yokeAttachmentName", "attachment" + IMAGE_MIMES.get(mime, "")
            )
            if not isinstance(name, str):
                raise ValueError("Invalid attachment name")
            attachments.append(InlineAttachment(attachment_name(name), mime, data))
        joined = "".join(text)
        if len(joined.encode()) > 1024 * 1024:
            raise ValueError("Prompt text exceeds the native limit")
        if not joined.strip() and not attachments:
            raise ValueError("Empty prompt requires explicit continuation")
        return DecodedPrompt(joined, attachments)
    except (ValueError, TypeError, AttributeError, binascii.Error) as exc:
        raise RequestError(-32602, str(exc)) from None


def history_content(block: dict[str, Any]) -> dict[str, Any]:
    """Project persisted image snapshots back to standard ACP content."""
    if block["type"] == "text":
        return block
    if block["type"] == "image":
        uri = block.get("uri") or ""
        if uri.startswith("data:") and ";base64," in uri:
            mime, data = uri[5:].split(";base64,", 1)
            return {
                "type": "image",
                "mimeType": mime,
                "data": data,
                "_meta": {"yokeAttachmentName": block["name"]},
            }
    raise RequestError(-32001, "Native history contains an unavailable content payload")
