"""Encode concise structured results and validated downstream image blocks."""

from __future__ import annotations

import base64
import json
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from yoke.mcp.results import mcp_result_text
from yoke.mcp_server.files import MAX_VIEW_IMAGE_BYTES, validate_image_bytes
from yoke.mcp_server.results.store import ResultStore


def encode(
    result: dict[str, Any],
    store: ResultStore,
    *,
    budget: int = 32000,
    legacy_text: bool = False,
) -> CallToolResult:
    content: list[Any] = []
    remaining = MAX_VIEW_IMAGE_BYTES
    value = dict(result)
    blocks = value.get("content")
    if isinstance(blocks, list):
        projected = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "image":
                data = block.get("data", "")
                if not isinstance(data, str) or len(data) > (remaining * 4 // 3 + 4):
                    raise ValueError("Downstream image exceeds response budget")
                decoded = base64.b64decode(data, validate=True)
                if len(decoded) > remaining:
                    raise ValueError("Downstream images exceed response budget")
                mime = validate_image_bytes(decoded)
                if mime != block.get("mimeType"):
                    raise ValueError("Downstream image MIME type does not match bytes")
                remaining -= len(decoded)
                content.append(ImageContent(type="image", data=data, mime_type=mime))
                projected.append(
                    {"type": "image", "mimeType": mime, "bytes": len(decoded)}
                )
            else:
                projected.append(block)
        value["content"] = mcp_result_text({"content": projected})
        value["media"] = [
            b for b in projected if isinstance(b, dict) and b.get("type") == "image"
        ]
    value = store.project(value, limit=budget)
    content.insert(
        0,
        TextContent(
            type="text",
            text=(
                "Completed. See structuredContent for results."
                if value.get("ok", True)
                else "Operation failed. See structuredContent for errors."
            ),
        ),
    )
    if legacy_text:
        content[0] = TextContent(
            type="text",
            text=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )
    return CallToolResult(
        content=content, structured_content=value, is_error=not value.get("ok", True)
    )
