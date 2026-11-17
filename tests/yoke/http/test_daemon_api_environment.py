"""Client ownership, actual shell isolation and privacy across daemon outputs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest


from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.command import ExecCommandTool
from yoke.environment import environment_values
from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
)


@pytest.fixture(autouse=True)
def api_environment_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "sessions" / "yoke.db"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


class EnvironmentProvider:
    def __init__(self, observed, barrier=None):
        self.observed = observed
        self.barrier = barrier
        self.expected = environment_values().get("YOKE_SENTINEL_X")
        self.credential = environment_values().get("FAKE_API_KEY")

    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            text = messages[-1].plain_text_content
            self.observed.append((self.expected, self.credential, text))
            if self.barrier is not None:
                self.barrier.wait(timeout=8)
            logging.getLogger(__name__).warning("Tool echoed %s", self.expected)
            return Message.assistant("checked " + str(self.expected))
        return Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="shell",
                    function=ToolFunction(
                        name="command_exec",
                        arguments=json.dumps(
                            {"argv": ["bash", "-c", "echo $YOKE_SENTINEL_X; pwd"]}
                        ),
                    ),
                )
            ],
        )


def make_app(tmp_path, observed, barrier=None):
    return create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: RuntimeAgent(
                EnvironmentProvider(observed, barrier),
                [ExecCommandTool.bind(root=Path(record.root or tmp_path))],
                tool_root=Path(record.root or tmp_path),
            ),
        )
    )
