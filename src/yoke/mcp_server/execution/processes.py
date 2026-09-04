"""Non-consuming process observation and explicit cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.mcp_server.execution.models import Request


class ProcessCursor(Request):
    session_id: int
    after_seq: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)


class ProcessRead(Request):
    sessions: list[ProcessCursor] = Field(min_length=1, max_length=16)
    wait_ms: int = Field(default=0, ge=0, le=30000)
    max_bytes: int = Field(default=32000, ge=1024, le=64000)


class ProcessCancel(Request):
    session_id: int


def page(
    manager: CommandProcessManager, cursor: ProcessCursor, budget: int
) -> dict[str, Any]:
    snapshot = manager.snapshot(cursor.session_id)
    output = manager.output_chunks(
        cursor.session_id, after_seq=cursor.after_seq, limit=100
    )
    text = ""
    seq, offset = cursor.after_seq, cursor.offset
    gap = cursor.after_seq < output.truncated_before_seq
    for chunk in output.chunks:
        start = offset if seq == cursor.after_seq and not gap else 0
        raw = chunk.text.encode("utf-8")
        if start > len(raw):
            raise ValueError("Invalid process cursor offset")
        available = max(0, budget - len(text.encode("utf-8")))
        fragment = raw[start : start + available].decode("utf-8", errors="ignore")
        text += fragment
        used = len(fragment.encode("utf-8"))
        if start + used < len(raw):
            offset = start + used
            break
        seq, offset = chunk.seq, 0
    return {
        "ok": True,
        "session_id": cursor.session_id,
        "status": snapshot.status,
        "exit_code": snapshot.exit_code,
        "output": text,
        "next_cursor": {
            "session_id": cursor.session_id,
            "after_seq": seq,
            "offset": offset,
        },
        "gap": gap,
        "truncated_before_seq": output.truncated_before_seq,
        "latest_seq": output.latest_seq,
    }


async def read(manager: CommandProcessManager, request: ProcessRead) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + request.wait_ms / 1000
    while True:
        results = []
        for cursor in request.sessions:
            try:
                results.append(
                    page(manager, cursor, request.max_bytes // len(request.sessions))
                )
            except ValueError as exc:
                results.append(
                    {"ok": False, "session_id": cursor.session_id, "error": str(exc)}
                )
        if asyncio.get_running_loop().time() >= deadline or any(
            r.get("output") or r.get("status") != "running" for r in results
        ):
            return {"ok": True, "items": results}
        await asyncio.sleep(0.05)
