from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
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


class ConcurrentFakeClient(FakeClient):
    def __init__(self, *, barrier: threading.Barrier | None = None) -> None:
        super().__init__()
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_requested=None,
    ) -> dict[str, Any]:
        del name, arguments, cancel_requested
        with self._active_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=1)
            else:
                time.sleep(0.05)
            return {}
        finally:
            with self._active_lock:
                self.active -= 1


def test_manager_serializes_calls_to_the_same_server(tmp_path: Path) -> None:
    client = ConcurrentFakeClient()
    manager = _manager(tmp_path, client)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                manager.call_tool,
                server="sample",
                tool="sample",
                arguments={},
            )
            for _ in range(2)
        ]
        assert all(future.result()["ok"] is True for future in futures)

    assert client.max_active == 1


def test_manager_allows_calls_to_different_servers_in_parallel(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    first = ConcurrentFakeClient(barrier=barrier)
    second = ConcurrentFakeClient(barrier=barrier)
    servers = (
        McpServerConfig(name="first", command="unused"),
        McpServerConfig(name="second", command="unused"),
    )
    manager = McpManager(McpConfig(servers), root=tmp_path)
    manager._clients = {"first": first, "second": second}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            manager.call_tool,
            server="first",
            tool="sample",
            arguments={},
        )
        second_future = executor.submit(
            manager.call_tool,
            server="second",
            tool="sample",
            arguments={},
        )
        assert first_future.result()["ok"] is True
        assert second_future.result()["ok"] is True


class BlockingFakeClient(FakeClient):
    def __init__(self, *, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_requested=None,
    ) -> dict[str, Any]:
        del name, arguments, cancel_requested
        self.started.set()
        assert self.release.wait(timeout=2)
        return {}


def test_manager_reloads_changed_config_after_in_flight_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir()
    config_path = config_dir / "mcp.json"

    def write_config(arg: str) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "mcp_servers": {
                        "sample": {
                            "command": "unused",
                            "args": [arg],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    write_config("one")
    started = threading.Event()
    release = threading.Event()
    created: list[tuple[McpServerConfig, FakeClient]] = []

    def create_client(server: McpServerConfig, *, root: Path) -> FakeClient:
        del root
        client: FakeClient
        if server.args == ("one",):
            client = BlockingFakeClient(started=started, release=release)
        else:
            client = FakeClient()
        created.append((server, client))
        return client

    monkeypatch.setattr("yoke.mcp.manager.create_mcp_client", create_client)
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            manager.call_tool,
            server="sample",
            tool="sample",
            arguments={},
        )
        assert started.wait(timeout=1)

        write_config("two")
        second = executor.submit(
            manager.call_tool,
            server="sample",
            tool="sample",
            arguments={},
        )
        time.sleep(0.05)
        assert second.done() is False

        release.set()
        assert first.result()["ok"] is True
        assert second.result()["ok"] is True

    assert [server.args for server, _client in created] == [("one",), ("two",)]
    assert created[0][1].closed is True
    assert created[1][1].closed is False


def test_manager_keeps_last_good_config_when_reload_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir()
    config_path = config_dir / "mcp.json"
    valid = json.dumps({"mcp_servers": {"sample": {"command": "unused"}}})
    config_path.write_text(valid, encoding="utf-8")
    client = FakeClient()
    monkeypatch.setattr(
        "yoke.mcp.manager.create_mcp_client", lambda _server, root: client
    )
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path)

    assert manager.inspect()["ok"] is True
    config_path.write_text("{", encoding="utf-8")

    invalid = manager.inspect()

    assert invalid["ok"] is False
    assert "MCP config reload failed" in str(invalid["error"])
    assert client.closed is False

    config_path.write_text(valid, encoding="utf-8")
    recovered = manager.inspect()

    assert recovered["ok"] is True
    assert client.closed is False
