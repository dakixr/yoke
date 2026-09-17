"""Encode concise structured results and validated downstream image blocks."""

from __future__ import annotations

import base64
import json
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from yoke.mcp.results import mcp_result_text
from yoke.mcp_server.files import MAX_VIEW_IMAGE_BYTES, validate_image_bytes
from yoke.mcp_server.results.store import ResultStore
from yoke.mcp_server.results.batch import project_batch


def encode(
    result: dict[str, Any],
    store: ResultStore,
    *,
    budget: int = 32000,
    legacy_text: bool = False,
    batch: bool = False,
    process: bool = False,
    process_recipe: bool = False,
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
    execution = value.get("execution")
    if process_recipe and isinstance(execution, dict) and _process_page(execution):
        # Recipes may carry an active runner beside a large patch report.
        # Retain the report if needed, but never conceal the runner's cursor.
        report = {key: item for key, item in value.items() if key != "execution"}
        try:
            report = store.project(report, limit=budget)
        except ValueError as exc:
            report = {
                "ok": value.get("ok", True),
                "report_omitted": True,
                "report_error": str(exc),
            }
        value = {**report, "execution": execution}
        process = True
    elif process and not _process_page(value):
        value = _project_process_outcome(value, store, budget)
    elif not process:
        value = (
            project_batch(value, store, budget)
            if batch
            else store.project(value, limit=budget)
        )
    # Only actual pages from dispatch-selected producers bypass preview limits.
    # Downstream results can contain arbitrary cursor/session_id-shaped data.
    # Shared process producers cap each call at 64,000 UTF-8 output bytes.
    # A second truncation here would advance a cursor past undelivered output.
    # Keep the whole page, including JSON escaping and cursor metadata overhead.
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
    if legacy_text or process:
        content[0] = TextContent(
            type="text",
            text=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )
    return CallToolResult(
        content=content, structured_content=value, is_error=not value.get("ok", True)
    )


def _process_page(value: dict[str, Any]) -> bool:
    if _process_item(value):
        return True
    items = value.get("items")
    return isinstance(items, list) and any(
        isinstance(item, dict) and _process_item(item) for item in items
    )


def _outcome(value: dict[str, Any]) -> dict[str, Any]:
    """Keep small operation outcomes outside projected, possibly huge error text."""
    outcome = {
        key: value[key]
        for key in (
            "ok",
            "session_id",
            "running",
            "exit_code",
            "timed_out",
            "cancelled",
            "input_written",
            "input_bytes_written",
        )
        if key in value and (value[key] is None or type(value[key]) in {bool, int})
    }
    if value.get("reason") in {
        "completed",
        "output",
        "deadline",
        "snapshot",
        "error",
        "cancelled",
    }:
        outcome["reason"] = value["reason"]
    if isinstance(value.get("items"), list):
        outcome["items"] = [
            _outcome(item) for item in value["items"][:16] if isinstance(item, dict)
        ]
    return outcome


def _project_process_outcome(
    value: dict[str, Any], store: ResultStore, budget: int
) -> dict[str, Any]:
    outcome = _outcome(value)
    reserve = len(json.dumps(outcome, ensure_ascii=True)) + 2
    try:
        projected = store.project(value, limit=max(0, budget - reserve))
    except ValueError:
        return {
            "ok": value.get("ok", True),
            "reason": "error",
            "truncated": True,
            "error": "Process result exceeds the available retention budget",
            **outcome,
        }
    if projected.get("truncated"):
        return {**projected, **outcome}
    return projected


def _process_item(value: dict[str, Any]) -> bool:
    cursor = value.get("cursor")
    session = value.get("session_id")
    return (
        type(session) is int
        and isinstance(cursor, str)
        and cursor.startswith("pc1_")
        and isinstance(value.get("output"), str)
        and isinstance(value.get("running"), bool)
        and "exit_code" in value
        and isinstance(value.get("has_more_output"), bool)
        and isinstance(value.get("gap"), bool)
    )
