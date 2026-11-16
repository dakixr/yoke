"""Client ownership, actual shell isolation and privacy across daemon outputs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from threading import Barrier

import pytest

from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.command import ExecCommandTool
from yoke.environment import environment_values
from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.http.test_daemon_api_events import open_stream
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
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


def attach(client, session, owner, sentinel, cwd, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "YOKE_SENTINEL_X": sentinel,
        "FAKE_API_KEY": "credential-" + sentinel,
    }
    result = client.put(
        f"/api/v1/session/{session}/client",
        headers=_auth(),
        json={
            "owner": owner,
            "env": env,
            "cwd": str(cwd),
            **extra,
        },
    )
    result.raise_for_status()
    assert sentinel not in result.text
    return env


def prompt(client, session, owner, text, **extra):
    result = client.post(
        f"/api/v1/session/{session}/prompt",
        headers=_auth(),
        json={
            "prompt": {"text": text},
            "clientID": owner,
            **extra,
        },
    )
    result.raise_for_status()
    return result.json()["data"]["id"]


def wait(client, session):
    result = client.post(
        f"/api/v1/session/{session}/wait?timeoutMs=10000", headers=_auth()
    )
    result.raise_for_status()
    assert result.json()["data"]["state"] == "idle", result.text
    client.post(
        f"/api/v1/session/{session}/drain?timeoutMs=10000", headers=_auth()
    ).raise_for_status()


def test_concurrent_client_envs_tools_credentials_and_no_secret_storage(
    tmp_path, caplog
):
    observed = []
    app = make_app(tmp_path, observed, Barrier(2))
    sentinels = ["sentinel-alpha-cdf379", "sentinel-beta-1a0462"]
    original = dict(os.environ)
    caplog.set_level(logging.WARNING)
    with TestClient(app) as client:
        for index, session in enumerate(("one", "two")):
            cwd = tmp_path / session
            cwd.mkdir()
            _create_session(client, tmp_path, session)
            client.patch(
                f"/api/v1/session/{session}", headers=_auth(), json={"title": session}
            ).raise_for_status()
            attach(client, session, session, sentinels[index], cwd)
        for session in ("one", "two"):
            prompt(client, session, session, "Print the environment variable")
        for session in ("one", "two"):
            wait(client, session)
        assert len(observed) == 2
        for expected, credential, text in observed:
            assert expected in text
            assert "credential-" + expected == credential
            assert (
                str(tmp_path / ("one" if expected == sentinels[0] else "two")) in text
            )
            for other in sentinels:
                if other != expected:
                    assert other not in text
        for session in ("one", "two"):
            history = client.get(
                f"/api/v1/session/{session}/history", headers=_auth()
            ).text
            messages = client.get(
                f"/api/v1/session/{session}/message", headers=_auth()
            ).text
            traces = str(
                app.state.runtime_registry.get_if_loaded(session).tool_traces.snapshot()
            )
            for sentinel in sentinels:
                assert sentinel not in history + messages + traces + caplog.text
    database_files = list((tmp_path / "sessions").rglob("yoke.db*"))
    assert database_files
    for path in database_files:
        for sentinel in sentinels:
            assert sentinel.encode() not in path.read_bytes(), path
    assert os.environ == original


def test_two_clients_share_prompts_detach_and_keep_admission_env_snapshot(tmp_path):
    observed = []
    app = make_app(tmp_path, observed)
    with TestClient(app) as client:
        _create_session(client, tmp_path, "one")
        client.patch(
            "/api/v1/session/one", headers=_auth(), json={"title": "shared"}
        ).raise_for_status()
        attach(client, "one", "alpha", "private-first-913ea", tmp_path)
        attach(client, "one", "beta", "private-second-1caa7", tmp_path)

        async def check():
            alpha = await open_stream(app, "one")
            beta = await open_stream(app, "one")
            try:
                first = await asyncio.to_thread(
                    prompt,
                    client,
                    "one",
                    "alpha",
                    "from alpha",
                    resume=False,
                    delivery="queue",
                )
                for iterator in (alpha, beta):
                    event = await anext(iterator)
                    assert "session.prompt.admitted" in event
                    assert '"clientID":"alpha"' in event
                    assert "from alpha" in event
                    assert first in event
                await asyncio.to_thread(
                    lambda: client.delete(
                        "/api/v1/session/one/client?owner=alpha", headers=_auth()
                    ).raise_for_status()
                )
                await asyncio.to_thread(
                    prompt, client, "one", "beta", "from beta", delivery="queue"
                )
                for iterator in (alpha, beta):
                    while True:
                        event = await anext(iterator)
                        if "session.prompt.admitted" in event:
                            break
                    assert '"clientID":"beta"' in event
                    assert "from beta" in event
            finally:
                await alpha.aclose()
                await beta.aclose()

        asyncio.run(check())
        wait(client, "one")
        assert [item[0] for item in observed] == [
            "private-first-913ea",
            "private-second-1caa7",
        ]
        status = client.get(
            "/api/v1/session/one/client?owner=beta", headers=_auth()
        ).json()["data"]
        assert status["owners"] == ["beta"]
        prompt(client, "one", "beta", "still attached")
        wait(client, "one")
        assert observed[-1][0] == "private-second-1caa7"
        # A browser without an explicit client uses the last attached environment.
        client.post(
            "/api/v1/session/one/prompt",
            headers=_auth(),
            json={"prompt": {"text": "web"}},
        ).raise_for_status()
        wait(client, "one")
        assert observed[-1][0] == "private-second-1caa7"
        # Once every attachment is gone, browser input retains the last env.
        client.delete(
            "/api/v1/session/one/client?owner=beta", headers=_auth()
        ).raise_for_status()
        assert (
            client.get("/api/v1/session/one/client", headers=_auth()).json()["data"]
            is None
        )
        client.post(
            "/api/v1/session/one/prompt",
            headers=_auth(),
            json={"prompt": {"text": "detached fallback"}},
        ).raise_for_status()
        wait(client, "one")
        assert observed[-1][0] == "private-second-1caa7"


def test_project_dotenv_and_python_env_are_session_local(tmp_path, monkeypatch):
    from yoke.agent.tools.python_env import (
        current_python_executable,
        prepare_python_env,
    )
    from yoke.environment import project_environment
    from yoke.mcp.config import McpServerConfig
    from yoke.mcp.environment import server_environment

    (tmp_path / ".env").write_text("YOKE_SENTINEL_X=dotenv-value\nPROJECT_ONLY=local\n")
    before = dict(os.environ)
    scoped = project_environment(tmp_path, {"YOKE_SENTINEL_X": "client-value"})
    assert scoped.values["YOKE_SENTINEL_X"] == "client-value"
    assert scoped.values["PROJECT_ONLY"] == "local"
    assert scoped.values["YOKE_HEADLESS"] == "1"
    assert os.environ == before
    venv = tmp_path / "venv"
    executable = venv / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    env = {"VIRTUAL_ENV": str(venv)}
    assert current_python_executable(env) == str(executable)
    prepare_python_env(env)
    assert env["YOKE_PYTHON_EXECUTABLE"] == str(executable)
    monkeypatch.setenv("YOKE_SENTINEL_X", "daemon-value")
    server = McpServerConfig(
        name="test", command="unused", env_vars=("YOKE_SENTINEL_X",)
    )
    assert (
        server_environment(server, scoped.values)["YOKE_SENTINEL_X"] == "client-value"
    )


def test_mcp_subprocess_inherits_each_explicit_environment(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from yoke.mcp.config import McpConfig, McpServerConfig
    from yoke.mcp.manager import McpManager

    server = tmp_path / "env_server.py"
    server.write_text("""import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    result = {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"env","version":"1"}}
    if request["method"] == "tools/list":
        result = {"tools":[{"name":"env","inputSchema":{"type":"object"}}]}
    if request["method"] == "tools/call":
        result = {"content":[{"type":"text","text":os.environ["YOKE_SENTINEL_X"]}]}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}), flush=True)
""")
    config = McpConfig(
        servers=(
            McpServerConfig(
                name="env",
                command=sys.executable,
                args=(str(server),),
                env_vars=("YOKE_SENTINEL_X",),
            ),
        )
    )

    def run(value):
        manager = McpManager(
            config, root=tmp_path, base_environment={"YOKE_SENTINEL_X": value}
        )
        try:
            return manager.call_tool(server="env", tool="env", arguments={})
        finally:
            manager.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(run, ("mcp-client-alpha", "mcp-client-beta")))
    assert "mcp-client-alpha" in str(first) and "mcp-client-beta" not in str(first)
    assert "mcp-client-beta" in str(second) and "mcp-client-alpha" not in str(second)


def test_provider_file_logs_and_validation_errors_do_not_echo_environment(tmp_path):
    from yoke.ai.providers.codex.subscription.logging import log_provider_event
    from yoke.http.services.privacy import SessionPrivacy

    privacy = SessionPrivacy()
    privacy.register({"YOKE_SENTINEL_X": "file-log-private-9274e"})
    try:
        with privacy.scope():
            log_provider_event(
                tmp_path, "fixture", "failed", error="file-log-private-9274e"
            )
        logs = list(tmp_path.glob("fixture-*.jsonl"))
        assert logs
        assert "file-log-private-9274e" not in logs[0].read_text()
        assert "<redacted>" in logs[0].read_text()
    finally:
        privacy.close()
    app = make_app(tmp_path, [])
    with TestClient(app) as client:
        _create_session(client, tmp_path, "one")
        result = client.put(
            "/api/v1/session/one/client",
            headers=_auth(),
            json={
                "owner": "one",
                "cwd": "invalid-private-cwd",
                "env": {"SECRET_TOKEN": "validation-private-value"},
            },
        )
        assert result.status_code == 422
        assert "invalid-private-cwd" not in result.text
        assert "validation-private-value" not in result.text
