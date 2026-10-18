"""Settlement sync uses durable T3 state, never the ACP disconnect itself."""

from __future__ import annotations

import asyncio
import json
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from yoke.http.services import thread_settlement as module
from yoke.mcp.client import McpClientError
from yoke.mcp.config import McpServerConfig
from yoke.mcp.http_client import StreamableHttpClient
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for
from tests.yoke.http.test_client_session import configure
from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session


def server():
    return {
        "name": "t3-code",
        "command": "/unused/t3",
        "env": {
            "T3_ACP_MCP_ENDPOINT": "http://t3/mcp",
            "T3_ACP_MCP_AUTHORIZATION": "Bearer ephemeral-test-secret",
        },
    }


def archived(client):
    return client.get("/api/v1/session/session-a", headers=_auth()).json()["data"][
        "archivedAt"
    ]


def reconcile(client, app):
    assert client.portal is not None
    client.portal.call(app.state.thread_settlement_service.reconcile, "session-a")


@pytest.mark.parametrize("settled_before_close", [False, True])
def test_settle_after_idle_disconnect_and_client_secret_cleanup(
    tmp_path, monkeypatch, settled_before_close
):
    state = {"threadId": "thread-a", "settled": settled_before_close}
    monkeypatch.setattr(module, "read_thread", lambda _: dict(state))
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        assert configure(client, mcpServers=[server()]).status_code == 200
        assert (
            client.delete(
                "/api/v1/session/session-a/client?owner=test-client", headers=_auth()
            ).status_code
            == 200
        )
        runtime = app.state.runtime_registry.get_if_loaded("session-a")
        assert runtime.resources.client_session is None
        assert runtime.resources._mcp_session_policy.injected_servers == ()
        if not settled_before_close:
            assert archived(client) is None
            assert "session-a" in app.state.thread_settlement_service.bindings
            state["settled"] = True
            reconcile(client, app)
        assert archived(client) is not None
        assert not app.state.thread_settlement_service.bindings
        assert all(
            b"ephemeral-test-secret" not in path.read_bytes()
            for path in (tmp_path / "sessions").rglob("*")
            if path.is_file()
        )
    assert not app.state.thread_settlement_service.bindings


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("offline"), McpClientError("MCP HTTP request failed: 403 Forbidden")],
)
def test_failed_read_never_settles(tmp_path, monkeypatch, failure):
    def read(_):
        raise failure

    monkeypatch.setattr(module, "read_thread", read)
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        reconcile(client, app)
        assert archived(client) is None
        retained = "session-a" in app.state.thread_settlement_service.bindings
        assert retained is not isinstance(failure, McpClientError)


def test_old_owner_and_missing_t3_server_cannot_settle(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(module, "read_thread", lambda binding: calls.append(binding))
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        client.delete(
            "/api/v1/session/session-a/client?owner=old-client", headers=_auth()
        )
        assert not calls
        configure(client)
        reconcile(client, app)
        assert not calls
        assert archived(client) is None


def test_queued_work_defers_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "read_thread", lambda _: {"threadId": "t", "settled": True}
    )
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        response = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "queued"}, "delivery": "queue", "resume": False},
        )
        assert response.status_code == 200
        reconcile(client, app)
        assert archived(client) is None
        assert "session-a" in app.state.thread_settlement_service.bindings


def test_replacing_client_during_read_prevents_stale_settlement(tmp_path, monkeypatch):
    started, release = Event(), Event()

    def read(_):
        started.set()
        assert release.wait(10)
        return {"threadId": "old", "settled": True}

    monkeypatch.setattr(module, "read_thread", read)
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        service = app.state.thread_settlement_service
        assert client.portal is not None
        future = client.portal.start_task_soon(service.reconcile, "session-a")
        assert started.wait(5)
        try:
            assert configure(client).status_code == 200
        finally:
            release.set()
        future.result(5)
        assert archived(client) is None


def test_retained_native_worker_defers_settlement_until_physical_drain(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        module, "read_thread", lambda _: {"threadId": "t", "settled": True}
    )
    app = app_for(tmp_path, RecordingProvider())
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])
        runtime = app.state.runtime_registry.get_if_loaded("session-a")
        assert client.portal is not None
        client.portal.call(runtime.workers.admit, 99)
        try:
            reconcile(client, app)
            assert archived(client) is None
            assert "session-a" in app.state.thread_settlement_service.bindings
        finally:
            client.portal.call(runtime.workers.release, 99)
        reconcile(client, app)
        assert archived(client) is not None


def test_mcp_http_contract_and_identity_validation(tmp_path, monkeypatch):
    calls = []
    thread_id = "thread-a"

    def respond(request):
        assert request.headers["authorization"] == "Bearer ephemeral"
        body = json.loads(request.content)
        calls.append(body)
        if body.get("method") != "initialize":
            assert request.headers["mcp-protocol-version"] == "2025-06-18"
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "t3", "version": "1"},
            }
        else:
            name = body["params"]["name"]
            data = {"threadId": thread_id}
            if name == "t3_thread_read":
                assert body["params"]["arguments"]["threadId"] == "thread-a"
                data = {"thread": {"threadId": thread_id, "settled": True}}
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def make_client(server, *, root):
        return StreamableHttpClient(
            server,
            root=root,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(module, "StreamableHttpClient", make_client)
    binding = module.ThreadBinding(
        "owner",
        McpServerConfig(
            name="t3-code",
            transport="streamable-http",
            url="http://t3/mcp",
            headers={"Authorization": "Bearer ephemeral"},
            cwd=tmp_path,
        ),
    )
    assert module.read_thread(binding)["settled"] is True
    binding.thread_id = "thread-a"
    thread_id = "wrong-thread"
    with pytest.raises(McpClientError, match="identity mismatch"):
        module.read_thread(binding)
    assert "ephemeral" not in repr(binding)


def test_watcher_reconciles_and_clears_credentials_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "read_thread", lambda _: {"threadId": "t", "settled": True}
    )
    app = app_for(tmp_path, RecordingProvider())
    service = app.state.thread_settlement_service
    service.interval = 0.01
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client, mcpServers=[server()])

        async def wait():
            async with asyncio.timeout(5):
                while service.bindings:
                    await asyncio.sleep(0.01)

        assert client.portal is not None
        client.portal.call(wait)
        assert archived(client) is not None
        configure(client, mcpServers=[server()])
    assert not service.bindings
