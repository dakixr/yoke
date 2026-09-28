"""Readable, bounded ACP display output; native structured results stay intact."""

from __future__ import annotations

import json
import re
from typing import Any

# Stay below clients' 8,000 UTF-16-unit tail limit, including Unicode and the
# marker, so client-side truncation does not remove the beginning again.
DISPLAY_BYTES = 7_500
_TRUNCATED = "\n\n[Display output truncated]\n\n"
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _render(value: Any, depth: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if depth < 8 and isinstance(value, list):
        if len(value) > 64:
            value = [*value[:32], f"[{len(value) - 64} entries omitted]", *value[-32:]]
        pieces = [_render(item, depth + 1) for item in value]
        return "\n".join(piece for piece in pieces if piece)
    if depth < 8 and isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        # Native rg matches are structured records, not already formatted lines.
        if isinstance(value.get("path"), str) and isinstance(value.get("text"), str):
            line = value.get("line")
            location = f"{value['path']}:{line}" if type(line) is int else value["path"]
            return f"{location}: {value['text']}"
        pieces: list[str] = []
        if value.get("error"):
            pieces.append("Error: " + _render(value["error"], depth + 1))
        if "session_id" in value and "status" in value:
            status = f"Process {value['session_id']} · {value['status']}"
            if value.get("exit_code") is not None:
                status += f" · exit {value['exit_code']}"
            if value.get("timed_out") is True:
                status += " · timed out"
            pieces.append(status)
        output = value.get("output")
        if isinstance(output, (str, list)):
            pieces.append(_render(output, depth + 1))
        elif "stdout" in value or "stderr" in value:
            pieces.extend(
                _render(value.get(key), depth + 1) for key in ("stdout", "stderr")
            )
        elif "content" in value:
            pieces.append(_render(value["content"], depth + 1))
        elif isinstance(value.get("items"), list):
            pieces.append(_render(value["items"], depth + 1))
        elif isinstance(value.get("result"), (dict, list)):
            pieces.append(_render(value["result"], depth + 1))
        if pieces:
            return "\n\n".join(piece for piece in pieces if piece)
    return json.dumps(value, ensure_ascii=False, indent=2)


def output_content(result: Any) -> list[dict[str, Any]]:
    """Keep text as text and put truncation between a useful head and tail."""
    text = _ANSI.sub("", _render(result))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > DISPLAY_BYTES:
        budget = DISPLAY_BYTES - len(_TRUNCATED.encode("utf-8"))
        head = budget // 2
        text = (
            encoded[:head].decode("utf-8", errors="ignore")
            + _TRUNCATED
            + encoded[-(budget - head) :].decode("utf-8", errors="ignore")
        )
    if not text:
        return []
    return [{"type": "content", "content": {"type": "text", "text": text}}]
