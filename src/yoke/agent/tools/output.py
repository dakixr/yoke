"""Global storage for complete tool outputs."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

TOOL_OUTPUT_RELATIVE_DIRECTORY = Path(".yoke") / "tool-output"
TOOL_OUTPUT_RETENTION_SECONDS = 7 * 24 * 60 * 60


def cleanup_expired_tool_outputs(home: Path, *, now: float | None = None) -> int:
    """Best-effort delete global tool outputs older than seven days."""
    try:
        output_directory = _output_directory(home)
        entries = list(output_directory.iterdir())
    except (OSError, ValueError):
        return 0
    cutoff = (time.time() if now is None else now) - TOOL_OUTPUT_RETENTION_SECONDS
    removed = 0
    for entry in entries:
        try:
            if entry.lstat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def save_markdown_tool_output(*, home: Path, source: str, content: str) -> str:
    """Atomically save complete Markdown content and return its absolute path."""
    output_directory = _output_directory(home)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / _markdown_filename(source)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_directory,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            if content and not content.endswith("\n"):
                temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return str(output_path.resolve())


def _output_directory(home: Path) -> Path:
    resolved_home = home.resolve()
    output_directory = resolved_home / TOOL_OUTPUT_RELATIVE_DIRECTORY
    resolved_output_directory = output_directory.resolve()
    try:
        resolved_output_directory.relative_to(resolved_home)
    except ValueError as exc:
        raise ValueError(
            "Tool output directory must stay inside the home directory"
        ) from exc
    return output_directory


def _markdown_filename(source: str) -> str:
    parsed = urlsplit(source)
    label = f"{parsed.netloc}{parsed.path}" if parsed.netloc else source
    readable = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()
    readable = readable[:80].rstrip("-") or "web-fetch"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}.md"
