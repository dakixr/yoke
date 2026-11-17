"""Exercise settlement discovery and failure policy over in-process MCP HTTP."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for
from tests.yoke.http.test_client_session import configure
from tests.yoke.http.test_runtime_lifetime_regressions import _create_session
from tests.yoke.http.test_thread_settlement import archived, reconcile, server
from yoke.http.services import thread_settlement as module
from yoke.mcp.config import McpServerConfig
from yoke.mcp.http_client import StreamableHttpClient

CURRENT_TOOLS = (
    "create_threads t3_thread_configure t3_thread_interrupt t3_thread_launch "
    "t3_thread_list t3_thread_organize t3_thread_read t3_thread_search "
    "t3_thread_send t3_thread_send_attachments t3_thread_transfer "
    "t3_thread_update t3_thread_wait"
).split()


class FakeT3:
    def __init__(self, tmp_path, monkeypatch):
        self.names = list(CURRENT_TOOLS)
        self.calls = []
        self.failure = None
        self.failure_at = "t3_thread_read"
        self.text_results = False
        self.state = {"threadId": "thread-a", "settled": True, "archived": False}
        self.binding = module.ThreadBinding(
            "owner",
            McpServerConfig(
                name="t3-code",
                transport="streamable-http",
                url="http://t3/mcp",
                headers={"Authorization": "Bearer ephemeral-test-secret"},
                cwd=tmp_path,
            ),
        )
        monkeypatch.setattr(module, "StreamableHttpClient", self.client)

    def client(self, config, *, root):
        return StreamableHttpClient(
            config,
            root=root,
            http_client=httpx.Client(transport=httpx.MockTransport(self.respond)),
        )

    def respond(self, request):
        assert request.headers["authorization"] == "Bearer ephemeral-test-secret"
        body = json.loads(request.content)
        method = body["method"]
        name = body.get("params", {}).get("name", method)
        self.calls.append(name)
        if name == self.failure_at and self.failure is not None:
            if isinstance(self.failure, Exception):
                raise self.failure
            if isinstance(self.failure, int):
                return httpx.Response(self.failure)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], **self.failure}
            )
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "t3", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": tool,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"threadId": {"type": "string"}},
                            "required": ["threadId"]
                            if tool == "t3_thread_read"
                            else [],
                        },
                    }
                    for tool in self.names
                ]
            }
        elif name not in self.names:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Unknown tool: " + name},
                },
            )
        else:
            args = body["params"]["arguments"]
            if name == "t3_thread_read":
                assert args == {"threadId": "thread-a", "limit": 1, "runLimit": 1}
                data = {"thread": self.state}
            else:
                assert args == {}  # Reading configuration must never mutate it.
                data = {"threadId": "thread-a"}
            result = (
                {"content": [{"type": "text", "text": json.dumps(data)}]}
                if self.text_results
                else {"structuredContent": data, "content": []}
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )


@pytest.fixture
def t3(tmp_path, monkeypatch):
    return FakeT3(tmp_path, monkeypatch)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("text_results", [False, True])
def test_discovers_scoped_thread_from_available_tools(t3, legacy, text_results):
    identity = "t3_thread_configuration" if legacy else "t3_thread_configure"
    if legacy:
        t3.names[t3.names.index("t3_thread_configure")] = identity
    t3.text_results = text_results
    assert module.read_thread(t3.binding) == t3.state
    assert t3.calls == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        identity,
        "t3_thread_read",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        {
            "error": {
                "code": -32601,
                "message": "Unknown tool http://private/ ephemeral-test-secret",
            }
        },
        {"error": {"code": -32602, "message": "Invalid params ephemeral-test-secret"}},
        {
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": "Unknown tool ephemeral-test-secret"}
                ],
            }
        },
        {
            "result": {
                "content": [
                    {"type": "text", "text": "invalid JSON ephemeral-test-secret"}
                ]
            }
        },
        400,
        401,
        403,
        404,
    ],
)
def test_protocol_failure_disables_once(tmp_path, t3, failure, caplog):
    t3.failure = failure
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        for _ in range(5):
            reconcile(client, app)
        assert "session-a" not in app.state.thread_settlement_service.bindings
        assert archived(client) is None
    assert t3.calls.count("t3_thread_read") == 1
    warnings = [r.message for r in caplog.records if "T3 settlement" in r.message]
    assert len(warnings) == 1
    assert "disabled" in warnings[0]
    assert "t3_thread_read" in warnings[0]
    assert "ephemeral-test-secret" not in caplog.text
    assert "http://" not in caplog.text


@pytest.mark.parametrize("missing", ["t3_thread_configure", "t3_thread_read"])
def test_missing_capability_disables_without_calling_it(tmp_path, t3, missing, caplog):
    t3.names.remove(missing)
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        for _ in range(3):
            reconcile(client, app)
        assert "session-a" not in app.state.thread_settlement_service.bindings
        assert archived(client) is None
    assert missing not in t3.calls
    assert t3.calls.count("tools/list") == 1
    warnings = [r.message for r in caplog.records if "T3 settlement" in r.message]
    assert len(warnings) == 1
    assert missing in warnings[0]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("ephemeral-test-secret"),
        httpx.ReadTimeout("offline"),
        429,
        503,
    ],
)
def test_transient_errors_back_off_and_recover(
    tmp_path, monkeypatch, t3, failure, caplog
):
    now = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0], raising=False)
    t3.failure = failure
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        service = app.state.thread_settlement_service
        for delay in (30, 60, 120, 240, 300, 300):
            before = t3.calls.count("t3_thread_read")
            reconcile(client, app)
            assert t3.calls.count("t3_thread_read") == before + 1
            assert archived(client) is None
            assert "session-a" in service.bindings
            now[0] += delay - 1
            reconcile(client, app)
            assert t3.calls.count("t3_thread_read") == before + 1
            now[0] += 1
        t3.failure = None
        reconcile(client, app)
        assert archived(client) is not None
        # A successful read restores the ordinary polling cadence.
        t3.state.update(settled=False, archived=False)
        reconcile(client, app)
        assert archived(client) is None
    assert "ephemeral-test-secret" not in caplog.text
    warnings = [r.message for r in caplog.records if "T3 settlement" in r.message]
    assert len(warnings) == 1


@pytest.mark.parametrize("capability", ["tools/list", "t3_thread_configure"])
def test_discovery_method_not_found_is_permanent(tmp_path, t3, capability, caplog):
    t3.failure_at = capability
    t3.failure = {"error": {"code": -32601, "message": "Method not found"}}
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        for _ in range(3):
            reconcile(client, app)
        assert "session-a" not in app.state.thread_settlement_service.bindings
        assert archived(client) is None
    assert t3.calls.count(capability) == 1
    warnings = [r.message for r in caplog.records if "T3 settlement" in r.message]
    assert len(warnings) == 1
    assert capability in warnings[0]


def test_cached_identity_mismatch_disables_without_archiving(tmp_path, t3, caplog):
    t3.state.update(settled=False)
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        reconcile(client, app)
        t3.state.update(threadId="wrong-thread", settled=True)
        reconcile(client, app)
        reconcile(client, app)
        assert "session-a" not in app.state.thread_settlement_service.bindings
        assert archived(client) is None
    assert t3.calls.count("t3_thread_configure") == 1
    assert t3.calls.count("t3_thread_read") == 2
    assert "t3_thread_read" in caplog.text


def test_reattach_keeps_backoff_until_credential_replacement(tmp_path, monkeypatch, t3):
    now = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0], raising=False)
    t3.failure = 503
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        reconcile(client, app)
        service = app.state.thread_settlement_service
        binding = service.bindings["session-a"]
        configure(client, mcpServers=[server()])
        reconcile(client, app)
        assert service.bindings["session-a"] is binding
        assert t3.calls.count("t3_thread_read") == 1
        # A newly scoped credential must discover its own identity and retry now.
        replacement = server()
        replacement["env"]["T3_ACP_MCP_ENDPOINT"] = "http://replacement/mcp"
        configure(client, mcpServers=[replacement])
        assert service.bindings["session-a"] is not binding
        t3.failure = None
        reconcile(client, app)
        assert archived(client) is not None
        assert t3.calls.count("t3_thread_configure") == 2
