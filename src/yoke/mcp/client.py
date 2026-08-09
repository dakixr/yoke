"""Minimal synchronous MCP stdio and Streamable HTTP clients."""

# ruff: noqa: D102, E402, E501, S603, UP037

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol


from yoke.mcp.config import McpServerConfig


JSON = dict[str, Any]
MCP_PROTOCOL_VERSION = "2025-03-26"


class McpClientError(RuntimeError):
    """Raised when an MCP client operation fails."""


@dataclass(slots=True, frozen=True)
class McpToolInfo:
    """Compact MCP tool metadata."""

    name: str
    description: str
    input_schema: JSON


class StdioMcpClient:
    """JSON-RPC MCP client for stdio servers."""

    def __init__(self, server: McpServerConfig, *, root: Path) -> None:
        self.server = server
        self.root = root.resolve()
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue[JSON]] = {}
        self._pending_lock = threading.Lock()
        self._failure_message: str | None = None
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = False
        self._tool_cache: tuple[McpToolInfo, ...] | None = None
        self._tools_changed = False
        self.server_instructions: str | None = None

    def start(self) -> None:
        """Start and initialize the MCP server."""
        if self._process is not None:
            return
        if self.server.transport != "stdio":
            raise McpClientError(
                f"MCP transport `{self.server.transport}` is not supported yet; use stdio"
            )
        if self.server.command is None:
            raise McpClientError("Missing MCP stdio command")
        env = {key: value for key, value in os.environ.items() if value is not None}
        for name in self.server.env_vars:
            if name in os.environ:
                env[name] = os.environ[name]
        if self.server.env:
            env.update(self.server.env)
        self._process = subprocess.Popen(
            [self.server.command, *self.server.args],
            cwd=str(self.server.cwd or self.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._closed = False
        with self._pending_lock:
            self._failure_message = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {"name": "yoke", "version": "0"},
                },
                timeout=self.server.startup_timeout_sec,
            )
            instructions = result.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                self.server_instructions = instructions.strip()
            self.notify("notifications/initialized")
        except BaseException:
            self.close()
            raise

    def list_tools(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        self.start()
        if self._tool_cache is not None and not force and not self._tools_changed:
            return self._tool_cache
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _page in range(100):
            params = {"cursor": cursor} if cursor else None
            result = self.request(
                "tools/list",
                params,
                timeout=self.server.tool_timeout_sec,
            )
            raw_tools = result.get("tools", [])
            if not isinstance(raw_tools, list):
                raise McpClientError("MCP tools/list returned invalid tools")
            for item in raw_tools:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                description = item.get("description")
                schema = item.get("inputSchema") or item.get("input_schema") or {}
                tools.append(
                    McpToolInfo(
                        name=name,
                        description=description if isinstance(description, str) else "",
                        input_schema=schema if isinstance(schema, dict) else {},
                    )
                )
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        else:
            raise McpClientError("MCP tools/list exceeded 100 pages")
        self._tool_cache = tuple(tools)
        self._tools_changed = False
        return self._tool_cache

    def call_tool(
        self,
        name: str,
        arguments: JSON,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        self.start()
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.server.tool_timeout_sec,
            cancel_requested=cancel_requested,
        )

    def request(
        self,
        method: str,
        params: JSON | None = None,
        *,
        timeout: float,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        """Send one JSON-RPC request and wait for a response."""
        request_id = self._allocate_id()
        response_queue: queue.Queue[JSON] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._failure_message is not None:
                raise McpClientError(self._failure_message)
            self._pending[request_id] = response_queue
        payload: JSON = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._send(payload)
            deadline = time.monotonic() + timeout
            while True:
                if cancel_requested is not None and cancel_requested():
                    self.notify("notifications/cancelled", {"requestId": request_id})
                    raise McpClientError(f"MCP request `{method}` cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpClientError(f"MCP request `{method}` timed out")
                try:
                    response = response_queue.get(timeout=min(0.01, remaining))
                    break
                except queue.Empty:
                    continue
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                message = error.get("message")
                raise McpClientError(str(message or error))
            raise McpClientError(str(error))
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise McpClientError(f"MCP request `{method}` returned a non-object result")
        return result

    def notify(self, method: str, params: JSON | None = None) -> None:
        payload: JSON = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def close(self) -> None:
        self._closed = True
        process = self._process
        self._process = None
        if process is None:
            return
        self._fail_pending("MCP client closed")
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, payload: JSON) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpClientError("MCP server is not running")
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._write_lock:
            process.stdin.write(line)
            process.stdin.flush()

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._closed:
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._handle_message(message)
        finally:
            self._fail_pending("MCP server stopped")

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for _line in process.stderr:
            if self._closed:
                break

    def _fail_pending(self, message: str) -> None:
        response = {"error": {"message": message}}
        with self._pending_lock:
            self._failure_message = message
            pending_requests = list(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            try:
                pending.put_nowait(response)
            except queue.Full:
                pass

    def _handle_message(self, message: JSON) -> None:
        message_id = message.get("id")
        if message_id is not None and ("result" in message or "error" in message):
            if isinstance(message_id, int):
                with self._pending_lock:
                    pending = self._pending.get(message_id)
                if pending is not None:
                    pending.put(message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        if message_id is not None:
            self._handle_server_request(message_id, method, message.get("params"))
            return
        if method == "notifications/tools/list_changed":
            self._tools_changed = True

    def _handle_server_request(
        self,
        message_id: object,
        method: str,
        params: object,
    ) -> None:
        del params
        if method == "roots/list":
            result = {
                "roots": [
                    {
                        "uri": self.root.as_uri(),
                        "name": self.root.name or str(self.root),
                    }
                ]
            }
            self._send({"jsonrpc": "2.0", "id": message_id, "result": result})
            return
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": message_id, "result": {}})
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32601,
                    "message": f"Unsupported MCP request: {method}",
                },
            }
        )


class McpClient(Protocol):
    """Protocol shared by stdio and Streamable HTTP MCP clients."""

    server_instructions: str | None

    def list_tools(self, *, force: bool = ...) -> tuple[McpToolInfo, ...]: ...

    def call_tool(
        self,
        name: str,
        arguments: JSON,
        *,
        cancel_requested: Callable[[], bool] | None = ...,
    ) -> JSON: ...

    def close(self) -> None: ...


from yoke.mcp.http_client import StreamableHttpClient


def create_mcp_client(server: McpServerConfig, *, root: Path) -> McpClient:
    """Create the appropriate MCP client for the server's transport."""
    if server.transport == "stdio":
        return StdioMcpClient(server, root=root)
    if server.transport in {"streamable-http", "http"}:
        return StreamableHttpClient(server, root=root)
    raise McpClientError(f"MCP transport `{server.transport}` is not supported yet")
