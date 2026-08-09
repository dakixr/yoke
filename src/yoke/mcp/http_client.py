"""Streamable HTTP MCP client."""

# ruff: noqa: E501, UP037

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

from yoke.mcp.client import JSON
from yoke.mcp.client import MCP_PROTOCOL_VERSION
from yoke.mcp.client import McpClientError
from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpServerConfig


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
        self._client = http_client or httpx.Client(
            timeout=server.tool_timeout_sec,
            transport=httpx.HTTPTransport(verify=server.verify),
        )
        self._owns_client = http_client is None
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 0
        self._tool_cache: tuple[McpToolInfo, ...] | None = None
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
        self.start()
        if self._tool_cache is not None and not force:
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
        return self._tool_cache

    def call_tool(
        self,
        name: str,
        arguments: JSON,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        """Call an MCP tool."""
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
        payload: JSON = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response_message = self._post_request(
            payload,
            timeout=timeout,
            cancel_requested=cancel_requested,
        )
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

    def _post_request(
        self,
        payload: JSON,
        *,
        timeout: float,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        with self._client.stream(
            "POST",
            self.url,
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        ) as response:
            finished = threading.Event()

            def close_on_cancel() -> None:
                if cancel_requested is None:
                    return
                while not finished.wait(0.005):
                    if cancel_requested():
                        response.close()
                        return

            threading.Thread(target=close_on_cancel, daemon=True).start()
            try:
                if response.status_code >= 400:
                    raise McpClientError(
                        "MCP HTTP request failed: "
                        f"{response.status_code} {response.reason_phrase}"
                    )
                self._capture_session_id(response)
                content_type = (
                    (response.headers.get("content-type") or "").split(";")[0].strip()
                )
                if content_type == "text/event-stream":
                    return _wait_for_response_in_sse(
                        response.iter_lines(),
                        payload.get("id"),
                        cancel_requested=cancel_requested,
                    )
                if content_type == "application/json":
                    response.read()
                    message = response.json()
                    if not isinstance(message, dict):
                        raise McpClientError("MCP HTTP response is not a JSON object")
                    return message
                raise McpClientError(
                    f"MCP HTTP unexpected content-type: {content_type or 'missing'}"
                )
            finally:
                finished.set()

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


def _wait_for_response_in_sse(
    lines,
    expected_id: object,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> JSON:
    """Parse an SSE stream and return the JSON-RPC response matching expected_id."""
    current_data: list[str] = []
    for line in lines:
        if cancel_requested is not None and cancel_requested():
            raise McpClientError("MCP HTTP request cancelled")
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
    if cancel_requested is not None and cancel_requested():
        raise McpClientError("MCP HTTP request cancelled")
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
