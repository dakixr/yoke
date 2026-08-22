"""Result compaction helpers for downstream MCP tool calls."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from typing import cast

from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import DEFAULT_MAX_LINES
from yoke.agent.truncate import format_size
from yoke.agent.truncate import truncate_head


def mcp_result_text(result: dict[str, object]) -> str:
    """Flatten downstream MCP content into text for the Yoke tool result."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            parts.append(_content_part_text(item))
    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(
            "Structured content:\n"
            + json.dumps(structured, indent=2, ensure_ascii=False)
        )
    if not parts:
        return json.dumps(result, indent=2, ensure_ascii=False)
    return "\n\n".join(part for part in parts if part)


def bounded_structured_content(
    structured: object, *, full_output_path: str | None
) -> object:
    """Bound structured content while retaining the full-output path."""
    if structured is None:
        return None
    serialized = json.dumps(structured, indent=2, ensure_ascii=False)
    truncation = truncate_head(
        serialized,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    if not truncation.truncated:
        return structured
    result: dict[str, object] = {
        "truncated": True,
        "truncation": truncation.to_dict(),
    }
    if full_output_path is not None:
        result["full_output_path"] = full_output_path
    return result


def truncate_result_text(text: str, *, server: str, tool: str) -> dict[str, object]:
    """Bound downstream text and persist the full payload when truncated."""
    truncation = truncate_head(
        text,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    file_path: str | None = None
    content = truncation.content
    if truncation.truncated:
        directory = Path(tempfile.gettempdir()) / "yoke-mcp"
        directory.mkdir(parents=True, exist_ok=True)
        safe = f"{int(time.time())}-{server}-{tool}".replace("/", "_")
        path = directory / f"{safe}.txt"
        path.write_text(text, encoding="utf-8")
        file_path = str(path)
        content = (
            content
            + "\n\n"
            + "[MCP output truncated: "
            + f"{truncation.output_lines} of {truncation.total_lines} lines, "
            + f"{format_size(truncation.output_bytes)} of {format_size(truncation.total_bytes)}. "
            + f"Full output saved to: {file_path}]"
        )
    return {
        "text": content,
        "file": file_path,
        "truncation": truncation.to_dict(),
    }


def _content_part_text(item: object) -> str:
    if not isinstance(item, dict):
        return str(item)
    item = cast(dict[str, object], item)
    item_type = item.get("type")
    if item_type == "text":
        text = item.get("text")
        return text if isinstance(text, str) else ""
    if item_type == "image":
        return f"[Image result: {item.get('mimeType', 'unknown')}]"
    if item_type == "audio":
        return f"[Audio result: {item.get('mimeType', 'unknown')}]"
    if item_type == "resource":
        resource = item.get("resource")
        if isinstance(resource, dict):
            resource = cast(dict[str, object], resource)
            uri = resource.get("uri", "unknown")
            text = resource.get("text")
            if isinstance(text, str):
                return f"[Resource: {uri}]\n{text}"
            return f"[Resource: {uri}]"
    return json.dumps(item, ensure_ascii=False)
