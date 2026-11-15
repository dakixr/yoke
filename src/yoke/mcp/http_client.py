"""Streamable HTTP MCP client."""

# ruff: noqa: E501, UP037

from __future__ import annotations

import json
import threading
from contextlib import suppress
from collections.abc import Callable
from pathlib import Path

import httpx

from yoke._tls import tls_verification_enabled
from yoke.mcp.config import McpServerConfig
from yoke.mcp.session import JSON, McpClientError, McpSession


class StreamableHttpClient(McpSession):
    """Synchronous MCP client for the Streamable HTTP transport."""

    def __init__(
        self,
        server: McpServerConfig,
        *,
        root: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(server, root=root)
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
            transport=httpx.HTTPTransport(
                verify=server.verify and tls_verification_enabled()
            ),
        )
        self._owns_client = http_client is None
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._next_id = 0

    def _open_transport(self) -> None:
        """The HTTP transport opens lazily on each request."""

    def _after_initialize(self, result: JSON) -> None:
        version = result.get("protocolVersion")
        if isinstance(version, str) and version:
            self._protocol_version = version

    def _close_transport(self) -> None:
        """Terminate the remote session and close owned HTTP resources."""
        if self._session_id is not None:
            with suppress(Exception):
                self._client.delete(self.url, headers=self._headers())
        if self._owns_client:
            self._client.close()
        self._session_id = None
        self._protocol_version = None

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
                        on_notification=self._handle_notification,
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
        if self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _capture_session_id(self, response: httpx.Response) -> None:
        session_id = response.headers.get("mcp-session-id")
        if isinstance(session_id, str) and session_id.strip():
            self._session_id = session_id.strip()

    def _handle_notification(self, message: JSON) -> None:
        if message.get("method") == "notifications/tools/list_changed":
            self._tools_changed = True

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id


def _wait_for_response_in_sse(
    lines,
    expected_id: object,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    on_notification: Callable[[JSON], None] | None = None,
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
                if on_notification is not None and "method" in message:
                    on_notification(message)
                if _message_matches_id(message, expected_id) and _is_response(message):
                    return message
            continue
        if line.startswith("data:"):
            data = line[len("data:") :]
            current_data.append(data[1:] if data.startswith(" ") else data)
    if current_data:
        message = _parse_sse_event(current_data)
        if message is not None:
            if on_notification is not None and "method" in message:
                on_notification(message)
            if _message_matches_id(message, expected_id) and _is_response(message):
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
