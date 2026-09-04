"""MCP-only orchestration over the shared process runtime and downstream clients."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from functools import partial
from typing import Any

from anyio.to_thread import run_sync
from mcp.types import Tool

from yoke.agent.tools.python_exec import PythonExecTool
from yoke.agent.tools.read import ReadTool
from yoke.mcp.manager import McpManager
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.execution import gateway, processes, reads, search, wrappers
from yoke.mcp_server.execution.bridge import ComposePython, PythonBridge
from yoke.mcp_server.execution.catalog import ACTIONS, descriptor
from yoke.mcp_server.execution.models import (
    BatchRead,
    DownstreamCall,
    Inspect,
    ResultRead,
)
from yoke.mcp_server.execution.processes import ProcessCancel, ProcessRead
from yoke.mcp_server.process_runtime import ProcessRuntime
from yoke.mcp_server.recipes.patch import CheckPatch, check_patch
from yoke.mcp_server.recipes.workspace import (
    SearchThenRead,
    WorkspaceSnapshot,
    search_then_read,
    snapshot,
)
from yoke.mcp_server.registry import effective_tool_registry
from yoke.mcp_server.results.store import ResultStore
from yoke.mcp_server.transfers.files import (
    ExportFile,
    FileTransfers,
    ImportFiles,
    WriteBinary,
)


class ExecutionService:
    def __init__(
        self, config: MCPServerConfig, runtime: ProcessRuntime, manager: McpManager
    ) -> None:
        self.config, self.runtime, self.manager = config, runtime, manager
        self.store = ResultStore()
        self.transfers = FileTransfers(config.root)
        self.wrappers = wrappers.load(config.wrappers_file)
        read_tools = tuple(
            f"{w.server}/{w.tool}:{w.digest}"
            for w in self.wrappers.values()
            if w.read_only
        )
        self.bridge = PythonBridge(self.dispatch, self.store, read_tools)
        self._orchestrations = asyncio.Semaphore(4)
        self._watchers: set[asyncio.Task[Any]] = set()
        self._sessions: dict[int, str] = {}

    def accepts(self, name: str) -> bool:
        return name in ACTIONS or name in self.wrappers

    def tools(self) -> dict[str, Tool]:
        result = {
            name: descriptor(name, action, self.defaults(name))
            for name, action in ACTIONS.items()
        }
        result.update(
            {name: wrapper.descriptor() for name, wrapper in self.wrappers.items()}
        )
        return result

    def defaults(self, name: str) -> dict[str, Any]:
        if name == "exec_python":
            return {
                "yield_time_ms": self.config.default_yield_ms,
                "timeout": self.config.python_timeout,
                "max_output_tokens": self.config.max_output_tokens,
            }
        return {}

    async def dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        if cancel and cancel.is_set():
            return {"ok": False, "status": "cancelled"}
        if name in self.wrappers:
            wrapper = self.wrappers[name]
            return await self.dispatch(
                "mcp_call",
                {
                    "server": wrapper.server,
                    "tool": wrapper.tool,
                    "schema_hash": wrapper.digest,
                    "arguments": arguments,
                },
                cancel=cancel,
            )
        if name not in ACTIONS:
            try:
                return await self.local(name, arguments, cancel=cancel)
            except (ValueError, OSError) as exc:
                return {"ok": False, "status": "error", "error": str(exc)}
        values = {**self.defaults(name), **arguments}
        request = ACTIONS[name].model.model_validate(values)
        if isinstance(request, BatchRead):
            return await self.batch(request)
        if isinstance(request, Inspect):
            return await run_sync(partial(gateway.inspect, self.manager, request))
        if isinstance(request, DownstreamCall):
            async with self.runtime._total:
                result = await run_sync(
                    partial(
                        gateway.call,
                        self.manager,
                        server=request.server,
                        tool=request.tool,
                        arguments=request.arguments,
                        expected_hash=request.schema_hash,
                        cancel_requested=cancel.is_set if cancel else None,
                    )
                )
            if request.fields:
                return {
                    "ok": result.get("ok", True),
                    **{k: result[k] for k in request.fields if k in result},
                }
            return result
        if isinstance(request, ComposePython):
            return await self.python(request)
        if isinstance(request, ResultRead):
            return self.store.read(
                request.result_ref,
                cursor=request.cursor,
                limit=request.limit,
                fields=request.fields,
            )
        if isinstance(request, ProcessRead):
            return await processes.read(self.runtime.manager, request)
        if isinstance(request, ProcessCancel):
            token = self._sessions.get(request.session_id)
            if token:
                self.bridge.revoke(token)
            state = self.runtime.manager.snapshot(request.session_id)
            if state.status == "running":
                await run_sync(
                    partial(self.runtime.manager.terminate, request.session_id)
                )
            return {
                "ok": True,
                "session_id": request.session_id,
                "status": "terminated" if state.status == "running" else state.status,
            }
        if isinstance(request, SearchThenRead):
            return await search_then_read(request, self.dispatch)
        if isinstance(request, WorkspaceSnapshot):
            return await snapshot(self.config.root, request, self.dispatch)
        if isinstance(request, CheckPatch):
            return await check_patch(
                self.config.root, request, self.dispatch, self.runtime._patch_lock
            )
        if isinstance(request, ImportFiles):
            async with self.runtime._total:
                return await run_sync(partial(self.transfers.imports, request))
        if isinstance(request, WriteBinary):
            async with self.runtime._total:
                return await run_sync(partial(self.transfers.write, request))
        if isinstance(request, ExportFile):
            async with self.runtime._total:
                return await run_sync(partial(self.transfers.export, request))
        raise ValueError("Unsupported action")

    async def local(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        spec = effective_tool_registry().get(name)
        if spec is None:
            raise ValueError("Unknown local tool")
        binding = {
            "root": self.config.root,
            "command_process_manager": self.runtime.manager,
            "skill_dirs": self.config.skill_dirs,
            "mcp_manager": self.manager,
            "cancel_requested": cancel.is_set if cancel else lambda: False,
        }
        if name == "read_file":
            request = ReadTool.model_validate(arguments)
            async with self.runtime._total:
                return await run_sync(
                    partial(reads.read_file, self.config.root, request.model_dump())
                )
        parsed = spec.tool_class.bind(**binding).parse_arguments(arguments)
        if name in {"rg", "fd"}:
            from yoke.mcp_server.search import MCPFdTool, MCPRipgrepTool

            assert isinstance(parsed, (MCPFdTool, MCPRipgrepTool))
            async with self.runtime._total:
                return await run_sync(partial(search.execute, parsed, cancel))
        return await self.runtime.execute(name, parsed)

    async def batch(self, request: BatchRead) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + request.deadline_ms / 1000
        semaphore = asyncio.Semaphore(request.max_concurrency)
        run_id = secrets.token_urlsafe(16)
        budget = min(64000, request.max_output_tokens * 4)
        item_budget = max(256, (budget - 1024) // len(request.items) - 256)

        async def item_result(item: Any) -> dict[str, Any]:
            async with semaphore:
                if time.monotonic() >= deadline:
                    return {
                        "id": item.id,
                        "status": "skipped",
                        "error": "Batch deadline expired",
                    }
                try:
                    cancelled = threading.Event()
                    try:
                        result = await self.local(
                            item.tool, item.arguments.model_dump(), cancel=cancelled
                        )
                    finally:
                        cancelled.set()
                    status = "ok" if result.get("ok", True) else "error"
                    return {
                        "id": item.id,
                        "status": status,
                        "data": self.store.project(result, limit=item_budget),
                    }
                except Exception as exc:
                    return {"id": item.id, "status": "error", "error": str(exc)[:256]}

        tasks = [asyncio.create_task(item_result(item)) for item in request.items]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=request.deadline_ms / 1000
            )
            for task in pending:
                task.cancel()
            settled = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results = [
            result
            if isinstance(result, dict)
            else {
                "id": item.id,
                "status": "cancelled",
                "error": "Batch deadline expired",
            }
            for item, result in zip(request.items, settled)
        ]
        return {
            "ok": True,
            "run_id": run_id,
            "items": results,
            "operations": sum(r["status"] != "skipped" for r in results),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    async def python(self, request: ComposePython) -> dict[str, Any]:
        async with self._orchestrations:
            token, run, code = await self.bridge.prepare(request)
            tool = PythonExecTool.bind(
                root=self.config.root,
                command_process_manager=self.runtime.manager,
                cancel_requested=run.cancelled.is_set,
            ).parse_arguments(
                {
                    **request.model_dump(exclude={"managed_calls", "max_calls"}),
                    "code": code,
                }
            )
            try:
                # Orchestration has separate admission: children need the operation slots.
                result = await run_sync(tool.execute)
            except BaseException:
                self.bridge.revoke(token)
                raise
            session = result.get("session_id")
            if isinstance(session, int):
                self._sessions[session] = token
                watcher = asyncio.create_task(self._watch(session, token, run.deadline))
                self._watchers.add(watcher)
                watcher.add_done_callback(self._watchers.discard)
            else:
                self.bridge.revoke(token)
            return result

    async def _watch(self, session: int, token: str, deadline: float) -> None:
        try:
            while time.monotonic() < deadline:
                if self.runtime.manager._get(session).finished:
                    return
                await asyncio.sleep(0.1)
        except ValueError:
            pass
        finally:
            self.bridge.revoke(token)
            self._sessions.pop(session, None)

    async def close(self) -> None:
        await self.bridge.close()
        for task in self._watchers:
            task.cancel()
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)
        self.store.close()
        self.transfers.close()
