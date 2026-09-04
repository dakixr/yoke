"""Version-one mechanical workspace recipes."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import Field

from yoke.mcp_server.execution.models import Request

Dispatch = Callable[..., Awaitable[dict[str, Any]]]


class SearchThenRead(Request):
    pattern: str = Field(min_length=1)
    root_dir: str | None = None
    max_files: int = Field(default=6, ge=1, le=16)
    context_lines: int = Field(default=5, ge=0, le=40)


class WorkspaceSnapshot(Request):
    paths: list[str] = Field(default_factory=list, max_length=16)
    pattern: str | None = None


async def search_then_read(
    request: SearchThenRead, dispatch: Dispatch
) -> dict[str, Any]:
    search = await dispatch(
        "rg",
        {
            "raw_args": "-n -- " + shlex.quote(request.pattern),
            "root_dir": request.root_dir,
            "max_output_chars": 64000,
        },
    )
    if not search.get("ok"):
        return {"ok": False, "recipe_version": 1, "search": search, "reads": []}
    output = search.get("output", [])
    reads = []
    seen: set[str] = set()
    if isinstance(output, list):
        for match in output:
            if not isinstance(match, dict):
                continue
            path = match.get("path") or match.get("file")
            line = match.get("line_number") or match.get("line") or 1
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            start = max(1, int(line) - request.context_lines)
            reads.append(
                await dispatch(
                    "read_file",
                    {
                        "path": path,
                        "offset": start,
                        "limit": request.context_lines * 2 + 1,
                    },
                )
            )
            if len(reads) >= request.max_files:
                break
    return {"ok": True, "recipe_version": 1, "search": search, "reads": reads}


async def snapshot(
    root: Path, request: WorkspaceSnapshot, dispatch: Dispatch
) -> dict[str, Any]:
    paths = list(dict.fromkeys(["AGENTS.md", *request.paths]))
    reads = await asyncio.gather(
        *(dispatch("read_file", {"path": path, "limit": 100}) for path in paths)
    )
    git = await asyncio.create_subprocess_exec(
        "git",
        "--no-optional-locks",
        "status",
        "--short",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(git.communicate(), 10)
    except BaseException:
        git.kill()
        await git.wait()
        raise
    result = {
        "ok": True,
        "recipe_version": 1,
        "root": str(root),
        "reads": reads,
        "git": {
            "exit_code": git.returncode,
            "output": stdout.decode(errors="replace")[:16000],
            "error": stderr.decode(errors="replace")[:2000],
        },
    }
    if request.pattern:
        result["search"] = await search_then_read(
            SearchThenRead(pattern=request.pattern), dispatch
        )
    return result


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
