"""Client configuration tested through real HTTP admissions and the agent loop."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.write import WriteTool
from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
)


class ToolProvider:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.arguments = arguments
        self.tools_seen: list[set[str]] = []
        self.messages_seen: list[list[Message]] = []
        self.calls = 0

    def complete(self, messages, tools):
        self.tools_seen.append({tool["function"]["name"] for tool in tools})
        self.messages_seen.append(list(messages))
        if messages[-1].role == "tool":
            return Message.assistant("Done", phase="final_answer")
        self.calls += 1
        return Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=f"call-{self.calls}",
                    function=ToolFunction(
                        name=self.name, arguments=json.dumps(self.arguments)
                    ),
                )
            ],
        )


def app_for(tmp_path: Path, provider: ToolProvider):
    return create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: RuntimeAgent(
                provider,
                [WriteTool.bind(root=Path(record.root or tmp_path))],
                tool_root=Path(record.root or tmp_path),
                tool_home=tmp_path,
            ),
        )
    )


def configure(client: TestClient, mode="full-access", **config):
    return client.put(
        "/api/v1/session/session-a/client",
        headers=_auth(),
        json={"owner": "test-client", "mode": mode, **config},
    )


def submit(client: TestClient):
    response = client.post(
        "/api/v1/session/session-a/prompt",
        headers=_auth(),
        json={"prompt": {"text": "Run the test tool"}},
    )
    assert response.status_code == 200, response.text


def drain(client: TestClient):
    response = client.post(
        "/api/v1/session/session-a/drain?timeoutMs=15000", headers=_auth()
    )
    assert response.json()["data"]["drained"], response.text


def pending(client: TestClient, kind: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(
            f"/api/v1/session/session-a/{kind}", headers=_auth()
        ).json()["data"]
        if result:
            return result[0]
        time.sleep(0.01)
    pytest.fail(f"No {kind} arrived")


@pytest.mark.parametrize(
    "mode,reply,written",
    [
        ("full-access", None, True),
        ("auto-accept-edits", None, True),
        ("plan", None, False),
        ("approval-required", "deny", False),
        ("approval-required", "allow", True),
    ],
)
def test_client_modes_gate_actual_file_writes(tmp_path, mode, reply, written):
    provider = ToolProvider("write", {"path": "result.txt", "content": "approved"})
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        assert configure(client, mode).status_code == 200
        submit(client)
        if reply:
            request = pending(client, "permission")
            assert request["toolCall"]["toolCallId"] == "call-1"
            assert not (tmp_path / "result.txt").exists()
            assert configure(client).status_code == 409
            assert (
                client.delete(
                    "/api/v1/session/session-a/client?owner=test-client",
                    headers=_auth(),
                ).status_code
                == 409
            )
            client.post(
                f"/api/v1/session/session-a/permission/{request['id']}/reply",
                headers=_auth(),
                json={"reply": reply},
            ).raise_for_status()
        drain(client)
        assert (tmp_path / "result.txt").exists() is written
        assert (
            client.get("/api/v1/session/session-a/permission", headers=_auth()).json()[
                "data"
            ]
            == []
        )


@pytest.mark.parametrize("answer", [True, False])
def test_question_answer_or_interrupt_drains_physical_workers(tmp_path, answer):
    provider = ToolProvider(
        "request_user_input", {"question": "Which option?", "options": ["one", "two"]}
    )
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client).raise_for_status()
        submit(client)
        request = pending(client, "question")
        if answer:
            client.post(
                f"/api/v1/session/session-a/question/{request['id']}/reply",
                headers=_auth(),
                json={"answers": ["two"]},
            ).raise_for_status()
        else:
            client.post(
                "/api/v1/session/session-a/interrupt", headers=_auth()
            ).raise_for_status()
        drain(client)
        assert not client.get(
            "/api/v1/session/session-a/question", headers=_auth()
        ).json()["data"]
        if answer:
            result = next(
                message
                for message in provider.messages_seen[-1]
                if message.role == "tool"
            )
            assert '"two"' in str(result.content)


def test_injected_stdio_mcp_survives_turn_fork_is_isolated_and_not_persisted(tmp_path):
    script = tmp_path / "mcp.py"
    script.write_text("""import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    method = request["method"]
    result = {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"test","version":"1"}}
    if method == "tools/list": result = {"tools":[{"name":"ping","description":"ping","inputSchema":{"type":"object"}}]}
    if method == "tools/call": result = {"content":[{"type":"text","text":"connected" if os.getenv("PRIVATE_TOKEN") == "client-secret-123" else "bad credential"}]}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}), flush=True)
""")
    provider = ToolProvider("mcp_call", {"server": "injected", "tool": "ping"})
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        response = configure(
            client,
            mcpServers=[
                {
                    "name": "injected",
                    "command": sys.executable,
                    "args": [str(script)],
                    "env": {"PRIVATE_TOKEN": "client-secret-123"},
                }
            ],
        )
        response.raise_for_status()
        assert "client-secret" not in response.text
        submit(client)
        drain(client)
        assert {
            "mcp_call",
            "mcp_inspect",
            "request_user_input",
            "update_plan",
        } <= provider.tools_seen[0]
        assert "connected" in str(provider.messages_seen[-1][-1].content)
        assert (
            "client-secret-123"
            not in client.get("/api/v1/session/session-a", headers=_auth()).text
        )
        listed = client.get("/api/v1/session/session-a/mcp", headers=_auth())
        assert listed.status_code == 200
        assert "injected" in listed.text and "client-secret" not in listed.text
        _create_session(client, tmp_path, "session-b")
        assert "injected" not in str(
            client.get("/api/v1/session/session-b/mcp", headers=_auth()).json()["data"]
        )
        # An unrelated owner cannot clear the current client's configuration.
        client.delete(
            "/api/v1/session/session-a/client?owner=other", headers=_auth()
        ).raise_for_status()
        assert (
            client.get("/api/v1/session/session-a/client", headers=_auth()).json()[
                "data"
            ]
            is not None
        )
        client.delete(
            "/api/v1/session/session-a/client?owner=test-client", headers=_auth()
        ).raise_for_status()
        assert (
            client.get("/api/v1/session/session-a/client", headers=_auth()).json()[
                "data"
            ]
            is None
        )
        assert "injected" not in str(
            client.get("/api/v1/session/session-a/mcp", headers=_auth()).json()["data"]
        )
    assert all(
        b"client-secret-123" not in path.read_bytes()
        for path in (tmp_path / "sessions").rglob("*")
        if path.is_file()
    )


def test_plans_remain_available_for_notification_reconciliation(tmp_path):
    provider = ToolProvider(
        "update_plan", {"plan": [{"step": "Implement", "status": "in_progress"}]}
    )
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        configure(client).raise_for_status()
        submit(client)
        drain(client)
        state = client.get("/api/v1/session/session-a/client", headers=_auth()).json()[
            "data"
        ]
        assert state["plan"]["steps"] == [
            {"step": "Implement", "status": "in_progress"}
        ]
        configure(client, "plan").raise_for_status()
        assert (
            client.get("/api/v1/session/session-a/client", headers=_auth()).json()[
                "data"
            ]["plan"]
            == state["plan"]
        )
