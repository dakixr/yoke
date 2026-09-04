"""Clean, bounded file data without changing the agent's ReadTool projection."""

from pathlib import Path
from typing import Any


def read_file(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = Path(arguments["path"]).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    offset = arguments.get("offset") or 1
    limit = arguments.get("limit") or 150
    if not path.is_file():
        raise ValueError("Path is not a regular file")
    # The internal limit is explicit. Never mistake a presentation placeholder for data.
    with path.open("rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("File exceeds 4 MiB internal read limit; use a bounded search")
    text = raw.decode("utf-8")
    if "\x00" in text:
        raise ValueError("File is not UTF-8 text")
    lines = text.split("\n")
    if offset > len(lines):
        raise ValueError("Offset is beyond end of file")
    end = min(len(lines), offset - 1 + limit)
    return {
        "ok": True,
        "path": str(path),
        "offset": offset,
        "limit": limit,
        "content": "\n".join(lines[offset - 1 : end]),
        "total_lines": len(lines),
        "complete": end == len(lines),
        "next_offset": end + 1 if end < len(lines) else None,
    }
