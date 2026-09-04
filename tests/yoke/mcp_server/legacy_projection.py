"""Frozen downstream presentation from 21dca9f for differential regression tests."""

from typing import cast
from yoke.mcp.results import (
    mcp_result_text,
    truncate_result_text,
    bounded_structured_content,
    persist_full_mcp_text,
    persist_full_mcp_result,
    add_full_output_path_to_text_result,
)


def project(result: dict[str, object], *, server: str, tool: str) -> dict[str, object]:
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
