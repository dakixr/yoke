"""Minimal synchronous MCP stdio and Streamable HTTP clients."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

import httpx

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

    def without_schema(self) -> McpToolInfo:
        """Return this tool metadata without input schema details."""
        if not self.input_schema:
            return self
        return McpToolInfo(
            name=self.name,
            description=self.description,
            input_schema={},
        )


class StdioMcpClient:
    """JSON-RPC MCP client for stdio servers."""

    def __init__(self, server: McpServerConfig, *, root: Path) -> None:
        self.server = server
        self.root = root.resolve()
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue[JSON]] = {}
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closed = False
        self._tool_cache: tuple[McpToolInfo, ...] | None = None
        self._tool_summary_cache: tuple[McpToolInfo, ...] | None = None
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
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
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

    def list_tools(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        """List all tools exposed by this MCP server."""
        if self._tool_cache is not None and not force and not self._tools_changed:
            return self._tool_cache
        self._tool_cache = self._list_tools(include_schemas=True)
        self._tool_summary_cache = tuple(
            tool.without_schema() for tool in self._tool_cache
        )
        self._tools_changed = False
        return self._tool_cache

    def list_tool_summaries(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        """List tool names/descriptions without retaining input schemas."""
        if (
            self._tool_summary_cache is not None
            and not force
            and not self._tools_changed
        ):
            return self._tool_summary_cache
        if self._tool_cache is not None and not force and not self._tools_changed:
            self._tool_summary_cache = tuple(
                tool.without_schema() for tool in self._tool_cache
            )
            return self._tool_summary_cache
        self._tool_summary_cache = self._list_tools(include_schemas=False)
        self._tools_changed = False
        return self._tool_summary_cache

    def _list_tools(self, *, include_schemas: bool) -> tuple[McpToolInfo, ...]:
        self.start()
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
                schema = (
                    (item.get("inputSchema") or item.get("input_schema") or {})
                    if include_schemas
                    else {}
                )
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
        return tuple(tools)

    def call_tool(self, name: str, arguments: JSON) -> JSON:
        """Call an MCP tool."""
        self.start()
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.server.tool_timeout_sec,
        )

    def request(
        self,
        method: str,
        params: JSON | None = None,
        *,
        timeout: float,
    ) -> JSON:
        """Send one JSON-RPC request and wait for a response."""
        request_id = self._allocate_id()
        response_queue: queue.Queue[JSON] = queue.Queue(maxsize=1)
        self._pending[request_id] = response_queue
        payload: JSON = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._send(payload)
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise McpClientError(f"MCP request `{method}` timed out") from exc
        finally:
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
        """Send one JSON-RPC notification."""
        payload: JSON = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def close(self) -> None:
        """Terminate the underlying server process."""
        self._closed = True
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        for pending in list(self._pending.values()):
            pending.put({"error": {"message": "MCP client closed"}})
        self._pending.clear()

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
        while not self._closed:
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            self._handle_message(message)

    def _handle_message(self, message: JSON) -> None:
        message_id = message.get("id")
        if message_id is not None and ("result" in message or "error" in message):
            if isinstance(message_id, int):
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

    def list_tool_summaries(self, *, force: bool = ...) -> tuple[McpToolInfo, ...]: ...

    def call_tool(self, name: str, arguments: JSON) -> JSON: ...

    def close(self) -> None: ...


class StreamableHttpClient:
    """Synchronous MCP client for the Streamable HTTP transport."""

    def __init__(
        self,
        server: McpServerConfig,
        *,
        root: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        if server.transport not in {"streamable-http", "http"}:
            raise McpClientError(
                f"StreamableHttpClient does not support transport `{server.transport}`"
            )
        if server.url is None:
            raise McpClientError("Missing MCP streamable-http url")
        self.server = server
        self.root = root.resolve()
        self.url = server.url
        self._client = http_client or httpx.Client(timeout=server.tool_timeout_sec)
        self._owns_client = http_client is None
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 0
        self._tool_cache: tuple[McpToolInfo, ...] | None = None
        self._tool_summary_cache: tuple[McpToolInfo, ...] | None = None
        self.server_instructions: str | None = None

    def start(self) -> None:
        """Initialize the MCP session over Streamable HTTP."""
        if self._initialized:
            return
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
        self._initialized = True

    def list_tools(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        """List all tools exposed by this MCP server."""
        if self._tool_cache is not None and not force:
            return self._tool_cache
        self._tool_cache = self._list_tools(include_schemas=True)
        self._tool_summary_cache = tuple(
            tool.without_schema() for tool in self._tool_cache
        )
        return self._tool_cache

    def list_tool_summaries(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        """List tool names/descriptions without retaining input schemas."""
        if self._tool_summary_cache is not None and not force:
            return self._tool_summary_cache
        if self._tool_cache is not None and not force:
            self._tool_summary_cache = tuple(
                tool.without_schema() for tool in self._tool_cache
            )
            return self._tool_summary_cache
        self._tool_summary_cache = self._list_tools(include_schemas=False)
        return self._tool_summary_cache

    def _list_tools(self, *, include_schemas: bool) -> tuple[McpToolInfo, ...]:
        self.start()
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
                schema = (
                    (item.get("inputSchema") or item.get("input_schema") or {})
                    if include_schemas
                    else {}
                )
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
        return tuple(tools)

    def call_tool(self, name: str, arguments: JSON) -> JSON:
        """Call an MCP tool."""
        self.start()
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.server.tool_timeout_sec,
        )

    def request(
        self,
        method: str,
        params: JSON | None = None,
        *,
        timeout: float,
    ) -> JSON:
        """Send one JSON-RPC request and wait for a response."""
        request_id = self._allocate_id()
        payload: JSON = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response_message = self._post_request(payload, timeout=timeout)
        if "error" in response_message:
            error = response_message["error"]
            if isinstance(error, dict):
                message = error.get("message")
                raise McpClientError(str(message or error))
            raise McpClientError(str(error))
        result = response_message.get("result", {})
        if not isinstance(result, dict):
            raise McpClientError(f"MCP request `{method}` returned a non-object result")
        return result

    def notify(self, method: str, params: JSON | None = None) -> None:
        """Send one JSON-RPC notification."""
        payload: JSON = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post_notification(payload, timeout=self.server.tool_timeout_sec)

    def close(self) -> None:
        """Terminate the session and close the HTTP client."""
        if self._session_id is not None:
            with suppress_exceptions:
                self._client.delete(
                    self.url,
                    headers=self._headers(),
                )
        if self._owns_client:
            self._client.close()
        self._session_id = None
        self._initialized = False

    def _post_request(self, payload: JSON, *, timeout: float) -> JSON:
        response = self._client.post(
            self.url,
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise McpClientError(
                f"MCP HTTP request failed: {response.status_code} {response.reason_phrase}"
            )
        self._capture_session_id(response)
        content_type = (
            (response.headers.get("content-type") or "").split(";")[0].strip()
        )
        if content_type == "text/event-stream":
            return _wait_for_response_in_sse(response.iter_lines(), payload.get("id"))
        if content_type == "application/json":
            message = response.json()
            if not isinstance(message, dict):
                raise McpClientError("MCP HTTP response is not a JSON object")
            return message
        raise McpClientError(
            f"MCP HTTP unexpected content-type: {content_type or 'missing'}"
        )

    def _post_notification(self, payload: JSON, *, timeout: float) -> None:
        response = self._client.post(
            self.url,
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise McpClientError(
                f"MCP HTTP notification failed: {response.status_code} {response.reason_phrase}"
            )
        self._capture_session_id(response)

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.server.headers:
            headers.update(self.server.headers)
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _capture_session_id(self, response: httpx.Response) -> None:
        session_id = response.headers.get("mcp-session-id")
        if isinstance(session_id, str) and session_id.strip():
            self._session_id = session_id.strip()

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id


class _SuppressExceptions:
    """Context manager that silently ignores exceptions."""

    def __enter__(self) -> "_SuppressExceptions":
        return self

    def __exit__(self, *_args: object) -> bool:
        return True


suppress_exceptions = _SuppressExceptions()


def _wait_for_response_in_sse(lines, expected_id: object) -> JSON:
    """Parse an SSE stream and return the JSON-RPC response matching expected_id."""
    current_data: list[str] = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if line == "":
            if current_data:
                message = _parse_sse_event(current_data)
                current_data = []
                if message is None:
                    continue
                if _message_matches_id(message, expected_id) and _is_response(message):
                    return message
            continue
        if line.startswith("data:"):
            data = line[len("data:") :]
            current_data.append(data[1:] if data.startswith(" ") else data)
    if current_data:
        message = _parse_sse_event(current_data)
        if (
            message is not None
            and _message_matches_id(message, expected_id)
            and _is_response(message)
        ):
            return message
    raise McpClientError("MCP SSE stream ended without a matching JSON-RPC response")


def _parse_sse_event(data_lines: list[str]) -> JSON | None:
    raw = "\n".join(data_lines)
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def _is_response(message: JSON) -> bool:
    return "result" in message or "error" in message


def _message_matches_id(message: JSON, expected_id: object) -> bool:
    message_id = message.get("id")
    if expected_id is not None:
        return message_id == expected_id
    return True


def create_mcp_client(server: McpServerConfig, *, root: Path) -> McpClient:
    """Create the appropriate MCP client for the server's transport."""
    if server.transport == "stdio":
        return StdioMcpClient(server, root=root)
    if server.transport in {"streamable-http", "http"}:
        return StreamableHttpClient(server, root=root)
    raise McpClientError(f"MCP transport `{server.transport}` is not supported yet")
