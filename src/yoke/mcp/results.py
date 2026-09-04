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
    """Flatten distinct downstream MCP content into text for the Yoke tool result."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            parts.append(_content_part_text(item))
    if parts:
        return "\n\n".join(part for part in parts if part)
    if result.get("structuredContent") is not None:
        return ""
    return json.dumps(result, indent=2, ensure_ascii=False)


def bounded_structured_content(structured: object) -> tuple[object, bool]:
    """Bound structured content and report whether truncation occurred."""
    if structured is None:
        return None, False
    serialized = json.dumps(structured, indent=2, ensure_ascii=False)
    truncation = truncate_head(
        serialized,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    if not truncation.truncated:
        return structured, False
    result: dict[str, object] = {
        "truncated": True,
        "truncation": truncation.to_dict(),
    }
    return result, True


def persist_full_mcp_text(text: str, *, server: str, tool: str) -> str:
    """Persist flattened downstream MCP text and return its local path."""
    path = _full_output_path(server=server, tool=tool, suffix=".txt")
    path.write_text(text, encoding="utf-8")
    return str(path)


def persist_full_mcp_result(
    result: dict[str, object], *, server: str, tool: str
) -> str:
    """Persist one complete downstream MCP result and return its local path."""
    path = _full_output_path(server=server, tool=tool, suffix=".json")
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return str(path)


def truncate_result_text(text: str) -> tuple[dict[str, object], bool]:
    """Bound downstream text without repeating retained content in metadata."""
    truncation = truncate_head(
        text,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    content = truncation.content
    if truncation.truncated:
        content = (
            content
            + "\n\n"
            + "[MCP output truncated: "
            + f"{truncation.output_lines} of {truncation.total_lines} lines, "
            + f"{format_size(truncation.output_bytes)} of {format_size(truncation.total_bytes)}."
            + "]"
        )
    return (
        {
            "text": content,
            "truncation": truncation.to_metadata_dict(),
        },
        truncation.truncated,
    )


def add_full_output_path_to_text_result(
    result: dict[str, object], full_output_path: str
) -> None:
    """Add a recovery path to an already-truncated text result in place."""
    text = result.get("text")
    if not isinstance(text, str) or not text.endswith("]"):
        return
    result["text"] = text[:-1] + f" Full output saved to: {full_output_path}]"


def _full_output_path(*, server: str, tool: str, suffix: str) -> Path:
    directory = Path(tempfile.gettempdir()) / "yoke-mcp"
    directory.mkdir(parents=True, exist_ok=True)
    safe = f"{time.time_ns()}-{server}-{tool}".replace("/", "_")
    return directory / f"{safe}{suffix}"


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


def project_tool_result(
    result: dict[str, object], *, server: str, tool: str
) -> dict[str, object]:
    """Preserve the established agent-facing downstream projection."""
    text = mcp_result_text(result)
    truncated, text_was_truncated = truncate_result_text(text)
    raw_structured = result.get("structuredContent")
    structured, structured_was_truncated = bounded_structured_content(raw_structured)
    full_output_path: str | None = None
    if text_was_truncated:
        full_output_path = persist_full_mcp_text(text, server=server, tool=tool)
        add_full_output_path_to_text_result(truncated, full_output_path)
    if structured_was_truncated:
        structured_path = persist_full_mcp_result(result, server=server, tool=tool)
        if isinstance(structured, dict):
            cast(dict[str, object], structured)["full_output_path"] = structured_path
        if full_output_path is None:
            full_output_path = structured_path
    return {
        "ok": not bool(result.get("isError")),
        "server": server,
        "tool": tool,
        "content": truncated["text"],
        "isError": bool(result.get("isError")),
        "structuredContent": structured,
        "truncation": truncated["truncation"],
        **(
            {"full_output_path": full_output_path}
            if full_output_path is not None
            else {}
        ),
    }
