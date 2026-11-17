"""Client configuration tested through real HTTP admissions and the agent loop."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.write import WriteTool
from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
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
