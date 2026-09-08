"""Provider-only projection for managed command tool results."""

from __future__ import annotations

import json


_COMMAND_RESULT_KEYS = {
    "ok",
    "session_id",
    "exit_code",
    "returncode",
    "running",
    "timed_out",
    "cancelled",
    "chunk_id",
    "wall_time_seconds",
    "elapsed_seconds",
    "original_token_count",
    "output",
    "outputTruncationDetails",
    "error",
    "command",
    "argv",
    "python_executable",
    "timeout",
}
_TRUNCATION_KEYS = {
    "truncated",
    "truncatedBy",
    "totalLines",
    "totalBytes",
    "outputLines",
    "outputBytes",
    "lastLinePartial",
    "firstLineExceedsLimit",
    "maxLines",
    "maxBytes",
}


def project_command_result(result: dict[str, object]) -> str | None:
    """Return lean JSON for one recognized managed-command result."""
    if not set(result).issubset(_COMMAND_RESULT_KEYS):
        return None
    truncation = result.get("outputTruncationDetails")
    if truncation is not None and (
        not isinstance(truncation, dict)
        or not set(truncation).issubset(_TRUNCATION_KEYS)
    ):
        return None
    if not any(
        key in result
        for key in (
            "outputTruncationDetails",
            "returncode",
            "command",
            "argv",
            "session_id",
            "python_executable",
        )
    ):
        return None
    ok = result.get("ok")
    if not isinstance(ok, bool):
        return None
    payload: dict[str, object] = {"ok": ok}
    output = result.get("output")
    if isinstance(output, str):
        payload["output"] = output

    if result.get("running") is True:
        payload["running"] = True
        session_id = result.get("session_id")
        if isinstance(session_id, int) and not isinstance(session_id, bool):
            payload["session_id"] = session_id

    exit_code = result.get("exit_code")
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        payload["exit_code"] = exit_code
    for key in ("timed_out", "cancelled"):
        if result.get(key) is True:
            payload[key] = True
    error = result.get("error")
    if isinstance(error, str) and error:
        payload["error"] = error

    if isinstance(truncation, dict) and truncation.get("truncated") is True:
        payload["truncated"] = True
        payload["kept"] = "tail"
        truncated_by = truncation.get("truncatedBy")
        if isinstance(truncated_by, str):
            payload["truncated_by"] = truncated_by
        for source, target in (
            ("outputLines", "visible_lines"),
            ("totalLines", "total_lines"),
        ):
            value = truncation.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                payload[target] = value
        if truncation.get("lastLinePartial") is True:
            payload["first_visible_line_partial"] = True
        original_tokens = result.get("original_token_count")
        if isinstance(original_tokens, int) and not isinstance(original_tokens, bool):
            payload["original_token_count"] = original_tokens

    python_executable = result.get("python_executable")
    if isinstance(python_executable, str) and python_executable:
        payload["python_executable"] = python_executable
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
