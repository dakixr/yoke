"""All client kinds use the same MCP credential and directory rules."""

import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.yoke.http.test_client_session import ToolProvider, app_for, configure
from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session
from yoke.mcp.client import create_mcp_client
from yoke.mcp.http_client import StreamableHttpClient

STDIO_SERVER = """
import json, os, sys
for line in sys.stdin:
    body = json.loads(line)
    if 'id' not in body:
        continue
    method = body['method']
    if method == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'check', 'inputSchema': {'type': 'object'}}]}
    else:
        result = {'content': [{'type': 'text', 'text': json.dumps({
            name: os.environ.get(name) for name in ['INHERITED_TOKEN', 'EXPLICIT_TOKEN', 'UNLISTED_SECRET', 'DAEMON_SECRET']
        })}]}
    print(json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}), flush=True)
"""


@pytest.mark.parametrize("kind", ["cli", "web", "acp"])
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_client_mcp_credentials_stay_scoped(tmp_path, monkeypatch, kind, transport):
    monkeypatch.setenv("DAEMON_SECRET", "daemon-private-value")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body["method"])
        assert request.headers["Authorization"] == "Bearer client-http-secret"
        if body["method"] != "initialize":
            assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "check", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "http checked"}]}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def connect(server, *, root, base_environment=None):
        if server.transport == "stdio":
            return create_mcp_client(
                server, root=root, base_environment=base_environment
            )
        return StreamableHttpClient(
            server,
            root=root,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr("yoke.mcp.manager.create_mcp_client", connect)
    provider = ToolProvider(
        "mcp_call", {"server": "fixture", "tool": "check", "arguments": {}}
    )
    app = app_for(tmp_path, provider)
    server: dict[str, object] = {"name": "fixture", "transport": transport}
    if transport == "stdio":
        server.update(
            command=sys.executable,
            args=["-c", STDIO_SERVER],
            env={"EXPLICIT_TOKEN": "explicit-client-value"},
            envVars=["INHERITED_TOKEN"],
        )
    else:
        server.update(
            url="http://fixture/mcp",
            headers={"Authorization": "Bearer client-http-secret"},
        )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        assert (
            configure(
                client,
                kind=kind,
                env={
                    "INHERITED_TOKEN": "inherited-client-value",
                    "UNLISTED_SECRET": "unlisted-client-value",
                },
                mcpServers=[server],
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "check"}},
            ).status_code
            == 200
        )
        result = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 2000},
        )
        assert result.json()["data"]["state"] == "idle"
        output = next(
            message.plain_text_content
            for message in provider.messages_seen[-1]
            if message.role == "tool"
        )
        assert output is not None
        if transport == "stdio":
            assert "inherited-client-value" in output
            assert "explicit-client-value" in output
            assert "unlisted-client-value" not in output
            assert "daemon-private-value" not in output
        else:
            assert "http checked" in output
            assert requests == [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
            ]
        status = client.get("/api/v1/session/session-a/client", headers=_auth()).text
        assert "client-http-secret" not in status
        assert "explicit-client-value" not in status
        # Tool output is transcript content. Attachment credentials themselves
        # must never become saved configuration or admission data.
        record = app.state.session_service.store.load("session-a")
        assert "mcpServers" not in record.model_dump_json()
        assert "client-http-secret" not in record.model_dump_json()


@pytest.mark.parametrize("kind", ["cli", "web", "acp"])
@pytest.mark.parametrize("directory", ["relative", "missing", "file"])
def test_client_directories_are_validated_for_every_kind(tmp_path, kind, directory):
    file = tmp_path / "file"
    file.write_text("file")
    value = "relative" if directory == "relative" else str(tmp_path / directory)
    with TestClient(app_for(tmp_path, ToolProvider("write", {}))) as client:
        _create_session(client, tmp_path, "session-a")
        response = configure(client, kind=kind, additionalDirectories=[value])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
