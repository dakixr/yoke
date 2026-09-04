"""Per-run capability dispatch over a private parent-owned Unix socket."""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import Field

from yoke.agent.tools.python_exec import PythonExecTool
from yoke.mcp_server.execution.models import Request
from yoke.mcp_server.results.store import ResultStore


class ManagedCall(Request):
    server: str
    tool: str
    arguments: dict[str, Any]
    schema_hash: str
    max_calls: int = Field(default=1, ge=1, le=16)


class ComposePython(PythonExecTool):
    managed_calls: list[ManagedCall] = Field(
        default_factory=list,
        max_length=32,
        description="Exact downstream actions authorized as part of this execution. Writes must be fully specified here; nested calls cannot expand them.",
    )
    max_calls: int = Field(default=32, ge=1, le=64)


@dataclass
class Run:
    deadline: float
    remaining: int
    manifest: list[ManagedCall]
    cancelled: threading.Event = field(default_factory=threading.Event)
    bytes_remaining: int = 16 * 1024 * 1024
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class PythonBridge:
    def __init__(
        self,
        dispatch: Callable[..., Awaitable[dict[str, Any]]],
        store: ResultStore,
        read_tools: tuple[str, ...],
    ) -> None:
        self.dispatch = dispatch
        self.store = store
        self.read_tools = read_tools
        self._directory = tempfile.TemporaryDirectory(prefix="yoke-bridge-")
        self.address = str(Path(self._directory.name) / "rpc.sock")
        self._server: asyncio.Server | None = None
        self._start_lock = asyncio.Lock()
        self._runs: dict[str, Run] = {}

    async def prepare(self, request: ComposePython) -> tuple[str, Run, str]:
        async with self._start_lock:
            if self._server is None:
                self._server = await asyncio.start_unix_server(
                    self._handle, path=self.address, limit=4 * 1024 * 1024 + 1
                )
        token = secrets.token_urlsafe(32)
        run = Run(
            time.monotonic() + request.timeout,
            request.max_calls,
            [item.model_copy(deep=True) for item in request.managed_calls],
        )
        self._runs[token] = run
        code = (
            "import sys\nfrom yoke.mcp_server.execution import client as _yoke_client\n"
            f"_yoke_client.configure({self.address!r}, {token!r})\n"
            "sys.modules['yoke_mcp'] = _yoke_client\n"
            f"exec(compile({request.code!r}, '<yoke-program>', 'exec'))\n"
        )
        return token, run, code

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        run: Run | None = None
        task = asyncio.current_task()
        try:
            raw = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(raw)
            run = self._runs.get(request["token"])
            if (
                run is None
                or run.cancelled.is_set()
                or time.monotonic() >= run.deadline
            ):
                raise ValueError("Unknown, cancelled or expired execution")
            if task:
                run.tasks.add(task)
            if run.remaining <= 0:
                raise ValueError("Execution operation budget exhausted")
            run.remaining -= 1
            run.bytes_remaining -= len(raw)
            if run.bytes_remaining < 0:
                raise ValueError("Execution byte budget exceeded")
            operation, arguments = request["operation"], request["arguments"]
            if operation == "emit":
                result = {
                    "ok": True,
                    **self.store.put(arguments["data"]),
                    "data": self.store.project(
                        {"ok": True, "data": arguments["data"]}, limit=8000
                    ),
                }
            else:
                if operation == "call":
                    name = arguments["name"]
                    if name not in {"read_file", "rg", "fd", "skill", "result_read"}:
                        raise ValueError(
                            "Only local read tools are allowed through tools.call"
                        )
                    params = arguments["arguments"]
                elif operation == "mcp":
                    name, params = "mcp_call", arguments
                    capability = f"{params['server']}/{params['tool']}:{params.get('schema_hash')}"
                    if capability not in self.read_tools:
                        match = next(
                            (
                                m
                                for m in run.manifest
                                if m.server == params["server"]
                                and m.tool == params["tool"]
                                and m.arguments == params["arguments"]
                                and m.schema_hash == params.get("schema_hash")
                                and m.max_calls > 0
                            ),
                            None,
                        )
                        if match is None:
                            raise ValueError(
                                "Downstream operation requires an exact outer managed_calls entry or a server-configured read capability"
                            )
                        match.max_calls -= 1
                else:
                    raise ValueError("Unknown bridge operation")
                async with asyncio.timeout(max(0.001, run.deadline - time.monotonic())):
                    result = await self.dispatch(name, params, cancel=run.cancelled)
            encoded = json.dumps(result, ensure_ascii=True).encode() + b"\n"
            run.bytes_remaining -= len(encoded)
            if run.bytes_remaining < 0 or len(encoded) > 8 * 1024 * 1024:
                raise ValueError("Execution byte budget exceeded")
        except asyncio.CancelledError:
            encoded = b'{"bridge_error":"Execution cancelled"}\n'
        except Exception as exc:
            encoded = json.dumps({"bridge_error": str(exc)}).encode() + b"\n"
        finally:
            if run and task:
                run.tasks.discard(task)
        try:
            writer.write(encoded)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def revoke(self, token: str) -> None:
        run = self._runs.pop(token, None)
        if run:
            run.cancelled.set()
            for task in tuple(run.tasks):
                task.cancel()

    async def close(self) -> None:
        tasks = [task for run in self._runs.values() for task in run.tasks]
        for token in tuple(self._runs):
            self.revoke(token)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._directory.cleanup()
