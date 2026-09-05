"""Apply an explicit patch with file preconditions, then run named checks."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync
from pydantic import Field, model_validator

from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.apply_patch.parser import PatchParser
from yoke.agent.tools.apply_patch.types import UpdateFileOp
from yoke.mcp_server.execution.models import Request
from yoke.mcp_server.recipes.workspace import Dispatch, file_hash
from yoke.mcp_server.recipes.patch_job import prepare


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
    root: Path,
    request: CheckPatch,
    dispatch: Dispatch,
    patch_lock: asyncio.Lock,
    job_root: Path,
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
        directory = prepare(
            job_root,
            {
                "root": str(root),
                "checks": [c.model_dump() for c in request.checks],
                "timeout": request.timeout_seconds,
                "before": before,
            },
        )
        execution: dict[str, Any] = {}
        patch: dict[str, Any] = {"ok": False, "status": "not_applied"}
        released = False
        try:
            execution = await dispatch(
                "exec_python",
                {
                    "code": "from yoke.mcp_server.recipes.patch_job import run_job\n"
                    f"run_job({str(directory)!r})\n",
                    "timeout": len(request.checks) * request.timeout_seconds + 40,
                    "yield_time_ms": 250,
                    "max_output_tokens": 8000,
                },
            )
            if not execution.get("ok") or not execution.get("session_id"):
                raise RuntimeError("Verification runner could not be started")
            async with asyncio.timeout(10):
                while not (directory / "ready").exists():
                    await asyncio.sleep(0.02)
            # Readiness does not reserve files against external editors. Recheck
            # hashes after startup so edits during the handshake are preserved.
            for name, path in paths.items():
                if file_hash(path) != request.expected_hashes[name]:
                    raise ValueError(
                        f"File changed during verification startup: {name}"
                    )
            tool = ApplyPatchTool.bind(root=root).parse_arguments(
                {"input": request.input}
            )
            patch = await run_sync(tool.execute)
            if not patch.get("ok"):
                return {"ok": False, "patch": patch, "checks": [], "recipe_version": 1}
            (directory / "go").touch()
            released = True
            return {
                "ok": True,
                "recipe_version": 1,
                "patch": patch,
                "status": "running",
                "execution": execution,
                "report": "Use process_read for the child report containing check outcomes, final diff and hashes.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "recipe_version": 1,
                "status": "error" if patch.get("ok") else "skipped",
                "patch": patch,
                "execution": execution,
                "checks": [],
                "error": str(exc) or "Verification startup timed out",
            }
        finally:
            if not released:
                shutil.rmtree(directory, ignore_errors=True)
                session = execution.get("session_id")
                if session is not None:
                    await asyncio.shield(
                        dispatch("process_cancel", {"session_id": session})
                    )
