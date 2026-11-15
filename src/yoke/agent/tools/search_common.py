"""Shared helpers for typed native search tools."""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import threading
import time
from collections.abc import Callable
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


def run_search_process(
    command: list[str],
    cwd: Path,
    render: Callable[[str, str, list[str], int], dict[str, object]],
    *,
    cancel: threading.Event | None,
    capture_limit: int,
) -> dict[str, object]:
    """Run a search command with bounded, cancellable stdout/stderr capture."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    buffers = {"out": bytearray(), "err": bytearray()}
    truncated = False
    deadline = time.monotonic() + 20
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                    process.terminate()
                    return {
                        "ok": False,
                        "status": "cancelled"
                        if cancel and cancel.is_set()
                        else "error",
                        "error": "Search cancelled or timed out",
                    }
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(data)
                    if sum(len(value) for value in buffers.values()) >= capture_limit:
                        truncated = True
                        break
                if truncated:
                    break
        if truncated:
            process.terminate()
        code = process.wait(timeout=2)
        stdout = buffers["out"].decode("utf-8", errors="replace")
        stderr = buffers["err"].decode("utf-8", errors="replace")
        effective_code = 0 if truncated and code not in {0, 1} else code
        result = render(stdout, stderr, command, effective_code)
        if truncated:
            result["truncated"] = True
        result["complete"] = not result.get("truncated", False)
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def add_option(command: list[str], condition: bool, *values: str) -> None:
    """Append one optional command-line option when its condition is true."""
    if condition:
        command.extend(values)
