"""Apply an explicit patch with file preconditions, then run named checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync
from pydantic import Field, model_validator

from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.apply_patch.parser import PatchParser
from yoke.agent.tools.apply_patch.types import UpdateFileOp
from yoke.mcp_server.execution.models import Request
from yoke.mcp_server.recipes.workspace import Dispatch, file_hash


class Check(Request):
    name: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1, max_length=64)


class CheckPatch(Request):
    input: str = Field(min_length=1)
    expected_hashes: dict[str, str | None]
    checks: list[Check] = Field(min_length=1, max_length=8)
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @model_validator(mode="after")
    def validate_manifest(self) -> CheckPatch:
        paths = set()
        for operation in PatchParser(self.input).parse():
            paths.add(operation.path)
            if isinstance(operation, UpdateFileOp) and operation.move_to:
                paths.add(operation.move_to)
        if paths != set(self.expected_hashes):
            raise ValueError(
                "expected_hashes must cover exactly every patched source and destination; null means absent"
            )
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("Check names must be unique")
        return self


async def check_patch(
    root: Path, request: CheckPatch, dispatch: Dispatch, patch_lock: asyncio.Lock
) -> dict[str, Any]:
    paths = {name: (root / name).resolve() for name in request.expected_hashes}
    before: dict[str, str] = {}
    async with patch_lock:
        for name, path in paths.items():
            if file_hash(path) != request.expected_hashes[name]:
                return {
                    "ok": False,
                    "status": "skipped",
                    "error": f"File changed: {name}",
                    "checks": [],
                }
            if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Patch recipe files must be at most 4 MiB")
            before[name] = path.read_text() if path.exists() else ""
        tool = ApplyPatchTool.bind(root=root).parse_arguments({"input": request.input})
        patch = await run_sync(tool.execute)
    if not patch.get("ok"):
        return {"ok": False, "patch": patch, "checks": [], "recipe_version": 1}
    code = (
        "from yoke.mcp_server.recipes.check_runner import run\n"
        "from yoke_mcp import output\n"
        f"output.emit(run({str(root)!r}, {[c.model_dump() for c in request.checks]!r}, "
        f"{request.timeout_seconds!r}, {before!r}))\n"
    )
    execution = await dispatch(
        "exec_python",
        {
            "code": code,
            "timeout": len(request.checks) * request.timeout_seconds + 10,
            "yield_time_ms": 1000,
            "max_output_tokens": 8000,
        },
    )
    return {
        "ok": execution.get("ok", False),
        "recipe_version": 1,
        "patch": patch,
        "status": "running" if execution.get("session_id") else "complete",
        "execution": execution,
        "report": "The child emits check outcomes, final diff and hashes through output.emit; use process_read if running.",
    }
