"""Shared helpers for typed native search tools."""

from __future__ import annotations

import stat
import json
from datetime import datetime, timezone
from pathlib import Path


def split_nul_paths(stdout: str) -> list[str]:
    """Split NUL-delimited paths without stripping legal CR/LF filename bytes."""
    items = stdout.split("\0")
    if items and items[-1] == "":
        items.pop()
    return [item for item in items if item]


def parse_nul_counts(stdout: str) -> list[dict[str, object]]:
    """Parse `path NUL count LF` records while preserving newlines in paths."""
    counts: list[dict[str, object]] = []
    position = 0
    while position < len(stdout):
        nul = stdout.find("\0", position)
        if nul < 0:
            break
        newline = stdout.find("\n", nul + 1)
        if newline < 0:
            newline = len(stdout)
        path = stdout[position:nul]
        raw_count = stdout[nul + 1 : newline]
        try:
            count = int(raw_count)
        except ValueError:
            break
        counts.append({"path": path, "count": count})
        position = newline + 1
    return counts


def parse_rg_json_items(stdout: str, *, only_matching: bool) -> list[dict[str, object]]:
    """Parse ripgrep JSON match/context events into stable structured items."""
    matches: list[dict[str, object]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("type") not in {
            "match",
            "context",
        }:
            continue
        kind = str(event.get("type"))
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path_data = data.get("path")
        path = path_data.get("text") if isinstance(path_data, dict) else ""
        lines_data = data.get("lines")
        text = lines_data.get("text") if isinstance(lines_data, dict) else None
        if text is None and isinstance(lines_data, dict):
            text = str(lines_data.get("bytes") or "")
        submatches = []
        for submatch in data.get("submatches") or []:
            if not isinstance(submatch, dict):
                continue
            match_data = submatch.get("match")
            match_text = (
                match_data.get("text") if isinstance(match_data, dict) else None
            )
            submatches.append(
                {
                    "text": str(match_text or ""),
                    "start": submatch.get("start"),
                    "end": submatch.get("end"),
                }
            )
        if only_matching and kind == "context":
            continue
        if only_matching:
            for submatch in submatches:
                matches.append(
                    {
                        "kind": "match",
                        "path": path,
                        "line": data.get("line_number"),
                        **submatch,
                    }
                )
            continue
        matches.append(
            {
                "kind": kind,
                "path": path,
                "line": data.get("line_number"),
                "text": str(text or "").rstrip("\n"),
                "submatches": submatches,
            }
        )
    return matches


def path_detail(value: str, root: Path) -> dict[str, object]:
    """Return stable metadata for one fd result path."""
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        info = path.lstat()
    except OSError as exc:
        return {"path": value, "error": str(exc)}
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        kind = "directory"
    elif stat.S_ISLNK(mode):
        kind = "symlink"
    elif stat.S_ISREG(mode):
        kind = "file"
    elif stat.S_ISSOCK(mode):
        kind = "socket"
    elif stat.S_ISFIFO(mode):
        kind = "pipe"
    else:
        kind = "other"
    return {
        "path": value,
        "type": kind,
        "size_bytes": info.st_size,
        "modified_at": datetime.fromtimestamp(
            info.st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def bound_text(text: str, limit: int) -> tuple[str, bool]:
    """Bound diagnostic text without changing it when already within budget."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True
