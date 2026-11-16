"""Startup selection uses CLI precedence and client credential resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.http.test_runtime_lifetime_regressions import TOKEN, _auth


@pytest.fixture(autouse=True)
def api_environment_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "sessions" / "yoke.db"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


PLUGIN = """
import json
from types import SimpleNamespace
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import ProviderModelInfo
PROVIDER_NAME = "fixture"

class Provider:
    supports_image_inputs = False
    def __init__(self, context):
        self.config = SimpleNamespace(model=context.model or "default", reasoning_effort=context.reasoning_effort)
        self.env = dict(context.env)
        if self.env.get("FAKE_API_KEY") != "fixture-client-key":
            raise ValueError("client credential missing")
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            if self.env.get("YOKE_SENTINEL_X") not in messages[-1].plain_text_content:
                raise ValueError("wrong tool environment")
            return Message.assistant("Verified client environment and credential.")
        return Message(role="assistant", tool_calls=[ToolCall(id="shell", function=ToolFunction(
            name="command_exec", arguments=json.dumps({"argv": ["bash", "-c", "echo $YOKE_SENTINEL_X"]})
        ))])

def register_provider(context):
    return Provider(context)

def list_provider_models(context):
    return [ProviderModelInfo(id=name, display_name=name, context_window_tokens=32000,
        thinking_levels=("low", "high"), default_thinking_level="low")
        for name in ("default", "user", "project", "flag")]
"""


@pytest.fixture
def startup_app(tmp_path, monkeypatch):
    providers = Path.home() / ".yoke" / "providers"
    providers.mkdir(parents=True)
    (providers / "fixture.py").write_text(PLUGIN)
    # The process has a conflicting key. Only the supplied client env is valid.
    monkeypatch.setenv("FAKE_API_KEY", "wrong-daemon-key")
    return create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )


def config(root, model):
    path = root / ".yoke" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "default_model": "fixture:" + model,
                "default_reasoning_effort": "high",
                "capabilities": {"shell": "allow"},
            }
        )
    )
    return path


def creation(tmp_path, **extra):
    return {
        "location": {"directory": str(tmp_path)},
        "startup": True,
        "client": {
            "owner": "terminal",
            "cwd": str(tmp_path),
            "env": {
                "PATH": os.environ["PATH"],
                "FAKE_API_KEY": "fixture-client-key",
                "YOKE_SENTINEL_X": "startup-sentinel-7a45d",
            },
        },
        **extra,
    }


@pytest.mark.parametrize("source", ["default", "user", "project", "flag"])
def test_startup_precedence_skill_report_and_real_credential_resolver(
    tmp_path, startup_app, source
):
    if source != "default":
        config(Path.home(), "user")
    else:
        path = tmp_path / ".yoke" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"capabilities":{"shell":"allow"}}')
    if source in {"project", "flag"}:
        config(tmp_path, "project")
    skill = tmp_path / ".yoke" / "skills" / "hello" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: hello\ndescription: Test startup skill\n---\nFollow these instructions.\n"
    )
    selection = {"provider": "fixture"}
    if source == "flag":
        selection.update(model="flag", reasoningEffort="low")
    with TestClient(startup_app) as client:
        response = client.post(
            "/api/v1/session",
            headers=_auth(),
            json=creation(tmp_path, selection=selection, skills=["hello"]),
        )
        response.raise_for_status()
        result = response.json()
        assert result["data"]["selection"]["model"] == source
        assert result["data"]["selection"]["reasoningEffort"] == (
            "low" if source in {"flag", "default"} else "high"
        )
        assert result["startup"]["warnings"] == []
        assert result["startup"]["activeSkills"] == ["hello"]
        assert result["startup"]["toolReport"]["message"].startswith("Loaded ")
        assert "activeTools" in result["startup"]["toolReport"]
        assert "fixture-client-key" not in response.text
        session = result["data"]["id"]
        record = startup_app.state.session_service.store.load(session)
        assert [item.name for item in record.active_skills] == ["hello"]
        client.post(
            f"/api/v1/session/{session}/prompt",
            headers=_auth(),
            json={"prompt": {"text": "Check environment"}, "clientID": "terminal"},
        ).raise_for_status()
        waited = client.post(
            f"/api/v1/session/{session}/wait?timeoutMs=10000", headers=_auth()
        )
        assert waited.json()["data"]["state"] == "idle", waited.text
        messages = client.get(
            f"/api/v1/session/{session}/message", headers=_auth()
        ).text
        assert "Verified client environment and credential." in messages
        assert "startup-sentinel-7a45d" not in messages


def test_inherited_stale_model_returns_cli_warning_without_rewriting_config(
    tmp_path, startup_app
):
    path = config(tmp_path, "retired")
    original = path.read_bytes()
    with TestClient(startup_app) as client:
        response = client.post(
            "/api/v1/session", headers=_auth(), json=creation(tmp_path)
        )
        response.raise_for_status()
        result = response.json()
        assert result["data"]["selection"]["model"] == "default"
        assert result["startup"]["modelSource"] == "config"
        assert len(result["startup"]["warnings"]) == 1
        assert "Configured model fixture:retired" in result["startup"]["warnings"][0]
        assert path.read_bytes() == original


def test_repeated_startup_preserves_conversation_and_skills(tmp_path, startup_app):
    config(tmp_path, "project")
    with TestClient(startup_app) as client:
        body = creation(tmp_path, id="repeat", title="repeat")
        client.post("/api/v1/session", headers=_auth(), json=body).raise_for_status()
        client.post(
            "/api/v1/session/repeat/prompt",
            headers=_auth(),
            json={"prompt": {"text": "check"}, "clientID": "terminal"},
        ).raise_for_status()
        result = client.post(
            "/api/v1/session/repeat/wait?timeoutMs=10000", headers=_auth()
        )
        assert result.json()["data"]["state"] == "idle"
        prior = client.get("/api/v1/session/repeat/message", headers=_auth()).json()[
            "data"
        ]
        response = client.post("/api/v1/session", headers=_auth(), json=body)
        response.raise_for_status()
        assert response.json()["startup"]["modelSource"] == "session"
        assert (
            client.get("/api/v1/session/repeat/message", headers=_auth()).json()["data"]
            == prior
        )
