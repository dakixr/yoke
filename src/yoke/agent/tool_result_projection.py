"""Compact canonical tool results only when building provider context."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import cast

from yoke.agent.models import Message
from yoke.agent.tool_result_projection_command import project_command_result
from yoke.agent.tool_result_toon import toon_array
from yoke.agent.tool_result_toon import toon_field
from yoke.agent.tool_result_toon import toon_table

_SEARCH_RESULT_KEYS = {
    "ok",
    "command",
    "output",
    "exit_code",
    "truncated",
    "summary",
    "stderr",
    "error",
}
_APPLY_PATCH_RESULT_KEYS = {
    "ok",
    "changes",
    "changes_applied",
    "stdout",
    "stderr",
    "error",
}
_WEB_SEARCH_RESULT_KEYS = {
    "ok",
    "results",
    "provider",
    "exhausted",
    "requestedResults",
    "returnedResults",
    "note",
    "error",
    "query",
}
_RG_COUNT_ROW_KEYS = {"path", "count"}
_RG_MATCH_ROW_KEYS = {"kind", "path", "line", "text", "submatches", "start", "end"}
_RG_SUBMATCH_KEYS = {"text", "start", "end"}
_FD_DETAIL_ROW_KEYS = {"path", "type", "size_bytes", "modified_at", "error"}
_APPLY_PATCH_ROW_KEYS = {"action", "path", "move_from"}
_WEB_SEARCH_ROW_KEYS = {"title", "url", "domain", "sourceType", "snippet"}

ToolResultProjector = Callable[[dict[str, object]], str | None]


def project_tool_results_for_provider(
    messages: list[Message],
) -> list[Message]:
    """Return provider messages with selected tool results compacted."""
    projected: list[Message] = []
    for message in messages:
        projection_kind = message._provider_result_projection
        if (
            message.role != "tool"
            or not projection_kind
            or not isinstance(message.content, str)
        ):
            projected.append(message)
            continue
        content = project_tool_result_content(projection_kind, message.content)
        if content == message.content:
            projected.append(message)
            continue
        copied = message.model_copy(deep=True)
        copied.content = content
        projected.append(copied)
    return projected


def project_tool_result_content(projection_kind: str, content: str) -> str:
    """Project one canonical JSON result for the model, or return it unchanged."""
    projector = _PROJECTORS.get(projection_kind)
    if projector is None:
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(parsed, dict):
        return content
    try:
        projected = projector(cast(dict[str, object], parsed))
    except (TypeError, ValueError, OverflowError):
        return content
    return projected if projected is not None else content


def _project_rg(result: dict[str, object]) -> str | None:
    if not set(result).issubset(_SEARCH_RESULT_KEYS):
        return None
    if "command" not in result or "exit_code" not in result:
        return None
    if result.get("ok") is not True:
        return _project_search_error(result)
    output = result.get("output")
    if not isinstance(output, list):
        return None
    lines = ["ok: true"]
    if not output:
        lines.append("output: []")
    elif all(isinstance(item, str) for item in output):
        lines.append(toon_array("paths", cast(list[object], output)))
    elif all(isinstance(item, dict) for item in output):
        rows = cast(list[dict[str, object]], output)
        table = _rg_table(rows)
        if table is None:
            return None
        lines.append(table)
    else:
        return None
    _append_search_metadata(lines, result)
    return "\n".join(lines)


def _rg_table(rows: list[dict[str, object]]) -> str | None:
    if all("path" in row and "count" in row for row in rows):
        if any(not set(row).issubset(_RG_COUNT_ROW_KEYS) for row in rows):
            return None
        return toon_table("counts", rows, ("path", "count"))
    if not all(
        "kind" in row and "path" in row and "line" in row and "text" in row
        for row in rows
    ):
        return None
    if any(not set(row).issubset(_RG_MATCH_ROW_KEYS) for row in rows):
        return None
    has_submatches = any("submatches" in row for row in rows)
    if has_submatches and not all("submatches" in row for row in rows):
        return None
    flattened_rows: list[dict[str, object]] = []
    for row in rows:
        submatches = row.get("submatches")
        if submatches is None:
            flattened_rows.append(row)
            continue
        if not isinstance(submatches, list) or any(
            not isinstance(submatch, dict)
            or not set(submatch).issubset(_RG_SUBMATCH_KEYS)
            for submatch in submatches
        ):
            return None
        if row.get("kind") == "context":
            if submatches:
                return None
            match_text = match_start = match_end = None
        elif row.get("kind") == "match" and len(submatches) == 1:
            submatch = cast(dict[str, object], submatches[0])
            match_text = submatch.get("text")
            match_start = submatch.get("start")
            match_end = submatch.get("end")
        else:
            return None
        flattened_rows.append(
            {key: value for key, value in row.items() if key != "submatches"}
            | {
                "match": match_text,
                "start": match_start,
                "end": match_end,
            }
        )

    projected_rows = flattened_rows if has_submatches else rows
    has_non_match = any(row.get("kind") != "match" for row in projected_rows)
    has_any_offset = any("start" in row or "end" in row for row in projected_rows)
    has_offsets = all("start" in row and "end" in row for row in projected_rows)
    if has_any_offset and not has_offsets:
        return None
    fields: list[str] = []
    if has_non_match:
        fields.append("kind")
    fields.extend(("path", "line", "text"))
    if has_submatches:
        fields.append("match")
    if has_offsets:
        fields.extend(("start", "end"))
    return toon_table("matches", projected_rows, tuple(fields))


def _project_fd(result: dict[str, object]) -> str | None:
    if not set(result).issubset(_SEARCH_RESULT_KEYS):
        return None
    if "command" not in result or "exit_code" not in result:
        return None
    if result.get("ok") is not True:
        return _project_search_error(result)
    output = result.get("output")
    if not isinstance(output, list):
        return None
    lines = ["ok: true"]
    if not output:
        lines.append("paths: []")
    elif all(isinstance(item, str) for item in output):
        lines.append(toon_array("paths", cast(list[object], output)))
    elif all(isinstance(item, dict) for item in output):
        rows = cast(list[dict[str, object]], output)
        if any(not set(row).issubset(_FD_DETAIL_ROW_KEYS) for row in rows):
            return None
        fields = ["path", "type", "size_bytes", "modified_at"]
        if any("error" in row for row in rows):
            fields.append("error")
        normalized = [{field: row.get(field) for field in fields} for row in rows]
        lines.append(toon_table("paths", normalized, tuple(fields)))
    else:
        return None
    _append_search_metadata(lines, result)
    return "\n".join(lines)


def _project_apply_patch(result: dict[str, object]) -> str | None:
    if not set(result).issubset(_APPLY_PATCH_RESULT_KEYS):
        return None
    if result.get("ok") is not True:
        if "stderr" not in result:
            return None
        error = result.get("error")
        if isinstance(error, str):
            return _compact_json({"ok": False, "error": error})
        return None
    if "changes_applied" not in result or "stdout" not in result:
        return None
    changes = result.get("changes")
    if not isinstance(changes, list) or not all(
        isinstance(change, dict) for change in changes
    ):
        return None
    rows = cast(list[dict[str, object]], changes)
    if any(not set(row).issubset(_APPLY_PATCH_ROW_KEYS) for row in rows):
        return None
    fields = ["action", "path"]
    if any("move_from" in row for row in rows):
        fields.append("move_from")
    normalized = [{field: row.get(field) for field in fields} for row in rows]
    return "\n".join(
        [
            "ok: true",
            toon_table(
                "changes",
                normalized,
                tuple(fields),
                delimiter="\t",
            ),
        ]
    )


def _project_web_search(result: dict[str, object]) -> str | None:
    if not set(result).issubset(_WEB_SEARCH_RESULT_KEYS):
        return None
    if result.get("ok") is not True:
        return None
    if not isinstance(result.get("provider"), str):
        return None
    raw_results = result.get("results")
    if not isinstance(raw_results, list) or not all(
        isinstance(item, dict) for item in raw_results
    ):
        return None
    rows = cast(list[dict[str, object]], raw_results)
    if any(not set(row).issubset(_WEB_SEARCH_ROW_KEYS) for row in rows):
        return None
    fields = ("title", "url", "domain", "sourceType", "snippet")
    normalized = [{field: row.get(field) for field in fields} for row in rows]
    lines = ["ok: true"]
    provider = result.get("provider")
    if isinstance(provider, str):
        lines.append(toon_field("provider", provider))
    lines.append(toon_table("results", normalized, fields))
    for key in ("exhausted", "requestedResults", "returnedResults", "note"):
        if key in result:
            lines.append(toon_field(key, result[key]))
    return "\n".join(lines)


def _project_search_error(result: dict[str, object]) -> str:
    payload: dict[str, object] = {"ok": False}
    for key in ("error", "exit_code", "stderr", "truncated", "summary"):
        value = result.get(key)
        if value is not None and value != "":
            payload[key] = value
    return _compact_json(payload)


def _append_search_metadata(lines: list[str], result: dict[str, object]) -> None:
    for key in ("truncated", "summary", "stderr"):
        value = result.get(key)
        if value is not None and value != "":
            lines.append(toon_field(key, value))


def _compact_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


_PROJECTORS: dict[str, ToolResultProjector] = {
    "apply_patch": _project_apply_patch,
    "command": project_command_result,
    "fd": _project_fd,
    "rg": _project_rg,
    "web_search": _project_web_search,
}
