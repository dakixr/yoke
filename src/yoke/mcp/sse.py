"""MCP Server-Sent Events response parsing."""

from __future__ import annotations

import json
from collections.abc import Callable

from yoke.mcp.errors import McpClientError
from yoke.mcp.types import JSON


def wait_for_response_in_sse(
    lines,
    expected_id: object,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> JSON:
    """Parse an SSE stream and return the response matching expected_id."""
    current_data: list[str] = []
    for line in lines:
        if cancel_requested is not None and cancel_requested():
            raise McpClientError("MCP HTTP request cancelled")
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if line == "":
            if current_data:
                message = _parse_sse_event(current_data)
                current_data = []
                if message is not None and _matches_response(message, expected_id):
                    return message
            continue
        if line.startswith("data:"):
            data = line[len("data:") :]
            current_data.append(data[1:] if data.startswith(" ") else data)
    if current_data:
        message = _parse_sse_event(current_data)
        if message is not None and _matches_response(message, expected_id):
            return message
    raise McpClientError("MCP SSE stream ended without a matching JSON-RPC response")


def _parse_sse_event(data_lines: list[str]) -> JSON | None:
    try:
        message = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def _matches_response(message: JSON, expected_id: object) -> bool:
    matches_id = expected_id is None or message.get("id") == expected_id
    return matches_id and ("result" in message or "error" in message)
