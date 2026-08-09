from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

import json
from pathlib import Path
import sys
from typing import Any
from typing import cast

import pytest

from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.mcp.client import McpToolInfo
from yoke.mcp.client import McpClientError
from yoke.mcp.client import StdioMcpClient
from yoke.mcp.config import McpConfig
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import load_mcp_config
from yoke.mcp.http_client import StreamableHttpClient
from yoke.mcp.manager import McpManager


class FakeClient:
    server_instructions: str | None = None

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.result = result or {}
        self.close_error = close_error
        self.closed = False

    def list_tools(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        del force
        return (McpToolInfo("sample", "", {}),)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_requested=None,
    ) -> dict[str, Any]:
        del name, arguments, cancel_requested
        return self.result

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _manager(tmp_path: Path, client: FakeClient) -> McpManager:
    server = McpServerConfig(name="sample", command="unused")
    manager = McpManager(McpConfig((server,)), root=tmp_path)
    manager._clients[server.name] = client
    return manager


def test_streamable_http_can_disable_tls_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / ".yoke" / "mcp.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "sample": {
                        "transport": "streamable-http",
                        "url": "https://mcp.test",
                        "verify": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    server = load_mcp_config(root=tmp_path, home=tmp_path).servers[0]
    transport_options: dict[str, object] = {}

    def create_http_transport(**options: object) -> object:
        transport_options.update(options)
        return object()

    monkeypatch.setattr(
        "yoke.mcp.http_client.httpx.HTTPTransport",
        create_http_transport,
    )
    monkeypatch.setattr(
        "yoke.mcp.http_client.httpx.Client", lambda **_options: object()
    )

    StreamableHttpClient(server, root=tmp_path)

    assert server.verify is False
    assert transport_options["verify"] is False


def test_stdio_mcp_drains_stderr_and_reaps_process(tmp_path: Path) -> None:
    server_script = tmp_path / "server.py"
    server_script.write_text(
        "import json, sys\n"
        "sys.stderr.buffer.write(b'\\xff' + b'x' * 1_000_000)\n"
        "sys.stderr.flush()\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request:\n"
        "        continue\n"
        "    method = request.get('method')\n"
        "    result = {'tools': []} if method == 'tools/list' else {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
        "'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    client = StdioMcpClient(
        McpServerConfig(
            name="sample",
            command=sys.executable,
            args=("-u", str(server_script)),
            startup_timeout_sec=5,
        ),
        root=tmp_path,
    )

    assert client.list_tools() == ()
    process = client._process
    assert process is not None

    client.close()

    assert process.returncode is not None
    assert client._reader is not None and not client._reader.is_alive()
    assert client._stderr_reader is not None
    assert not client._stderr_reader.is_alive()


def test_stdio_mcp_rejects_requests_after_reader_failure(
    tmp_path: Path,
) -> None:
    client = StdioMcpClient(
        McpServerConfig(name="sample", command=sys.executable), root=tmp_path
    )
    client._fail_pending("reader failed")

    with pytest.raises(McpClientError, match="reader failed"):
        client.request("tools/list", timeout=1)


def test_manager_closes_every_client_after_close_failure(
    tmp_path: Path,
) -> None:
    first = FakeClient(close_error=RuntimeError("close failed"))
    second = FakeClient()
    manager = McpManager(McpConfig(()), root=tmp_path)
    manager._clients = {"first": first, "second": second}

    with pytest.raises(RuntimeError, match="close failed"):
        manager.close()

    assert first.closed is True
    assert second.closed is True
    assert manager._clients == {}


def test_manager_bounds_large_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    structured = {"payload": "x" * (DEFAULT_MAX_BYTES * 2)}
    manager = _manager(
        tmp_path,
        FakeClient({"content": [], "structuredContent": structured}),
    )

    result = manager.call_tool(server="sample", tool="sample", arguments={})

    bounded = result["structuredContent"]
    assert isinstance(bounded, dict)
    bounded = cast(dict[str, object], bounded)
    assert bounded["truncated"] is True
    assert "payload" not in bounded
    assert len(json.dumps(bounded)) < 2_000
    assert Path(str(result["full_output_path"])).is_file()


def test_manager_preserves_small_structured_content(tmp_path: Path) -> None:
    structured = {"answer": 42}
    manager = _manager(
        tmp_path,
        FakeClient({"content": [], "structuredContent": structured}),
    )

    result = manager.call_tool(server="sample", tool="sample", arguments={})

    assert result["structuredContent"] == structured
