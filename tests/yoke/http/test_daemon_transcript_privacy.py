"""Real terminal environments must not rewrite provider history or event content."""

from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.command import ExecCommandTool
from yoke.agent.tool_result_projection import project_tool_results_for_provider
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.runtime.entry_persistence import tag_input_entry
from yoke.http.services.event_broker import GlobalEventBroker
from yoke.agent.observability import ToolTraceStore
from yoke.session.repository.rows import encode_entry
from tests.yoke.http.test_daemon_api_environment import prompt, wait
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
)


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("echo_secret", [False, True])
def test_terminal_env_preserves_entries_events_and_resumed_provider_projection(
    tmp_path, monkeypatch, indexed, echo_secret
):
    database = tmp_path / "sessions" / "yoke.db"
    monkeypatch.setenv("YOKE_DB_PATH", str(database))
    sentinel = secrets.token_hex(10)
    secret = "sk-test-" + secrets.token_hex(12)
    mapping_only = "not-printed-" + secrets.token_hex(10)
    env = {
        **os.environ,
        "SHLVL": "1",
        "COLUMNS": "80",
        "LINES": "24",
        "MY_SHORT_SECRET": "1",
        "MY_API_KEY": secret,
        "YOKE_SENTINEL_X": sentinel,
        "ENV_MAPPING_ONLY": mapping_only,
    }
    requests, responses, results, events = [], [], [], []
    original_run = RuntimeAgent.run
    original_publish = GlobalEventBroker.publish

    def run(agent, *args, **kwargs):
        result = original_run(agent, *args, **kwargs)
        results.append(copy.deepcopy(result))
        return result

    def publish(broker, event, **kwargs):
        events.append(event.model_copy(deep=True))
        return original_publish(broker, event, **kwargs)

    monkeypatch.setattr(RuntimeAgent, "run", run)
    monkeypatch.setattr(GlobalEventBroker, "publish", publish)

    class Provider:
        def complete(self, messages, tools):
            requests.append([message.model_dump_json() for message in messages])
            if messages[-1].plain_text_content == "projection probe":
                response = Message.assistant("probe complete 1 80 24")
            elif messages[-1].role == "tool":
                response = Message.assistant("checked 1 80 24 " + sentinel)
            else:
                command = "echo $YOKE_SENTINEL_X; echo 1 80 24"
                if echo_secret:
                    command += "; echo $MY_API_KEY"
                response = Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="shell-1-80-24",
                            function=ToolFunction(
                                name="command_exec",
                                arguments=json.dumps(
                                    {"argv": ["bash", "-c", command], "login": False}
                                ),
                            ),
                        )
                    ],
                )
            responses.append(response.model_dump_json())
            return response

    def app():
        return create_app(
            HttpAppSettings(
                auth_token=TOKEN,
                session_directory=database.parent,
                indexed_runtime_seed=indexed,
                agent_factory=lambda record: RuntimeAgent(
                    Provider(),
                    [ExecCommandTool.bind(root=tmp_path)],
                    tool_root=tmp_path,
                ),
            )
        )

    def attach(client):
        response = client.put(
            "/api/v1/session/one/client",
            headers=_auth(),
            json={"owner": "terminal", "kind": "cli", "env": env, "cwd": str(cwd)},
        )
        response.raise_for_status()
        for response in (
            response,
            client.get("/api/v1/session/one/client?owner=terminal", headers=_auth()),
        ):
            assert secret not in response.text
            assert mapping_only not in response.text
            assert str(cwd) not in response.text
            assert '"env"' not in response.text

    cwd = tmp_path / "private-client-cwd"
    cwd.mkdir()
    first_app = app()
    with TestClient(first_app) as client:
        _create_session(client, tmp_path, "one")
        client.patch(
            "/api/v1/session/one", headers=_auth(), json={"title": "one"}
        ).raise_for_status()
        attach(client)
        input_id = prompt(client, "one", "terminal", "print 1 80 24")
        wait(client, "one")
        record = first_app.state.session_service.store.load("one")
        expected = results[-1].conversation_entries
        tag_input_entry(expected, input_id)
        # Compare codec bytes, including IDs, timestamps, messages and metadata.
        assert [encode_entry(entry) for entry in record.conversation_entries] == [
            encode_entry(entry) for entry in expected
        ]
        assert [message.model_dump_json() for message in record.messages] == [
            message.model_dump_json() for message in results[-1].messages
        ]
        assert requests[1] == [
            message.model_dump_json()
            for message in project_tool_results_for_provider(results[-1].messages[:-1])
        ]
        assert responses[:2] == [
            message.model_dump_json()
            for message in record.messages
            if message.role == "assistant"
        ]
        tool = next(entry for entry in expected if entry.kind == "tool_result")
        output = json.loads(tool.message.plain_text_content)["output"]
        assert output == sentinel + "\n1 80 24\n" + (
            secret + "\n" if echo_secret else ""
        )
        messages = client.get("/api/v1/session/one/message", headers=_auth()).json()[
            "data"
        ]
        public_tool = next(item for item in messages if item["kind"] == "tool_result")
        assert public_tool["result"] == tool.message.display_text_content()
        assert public_tool["id"] == tool.id
        assert public_tool["timeCreated"] == tool.created_at
        ended = next(event for event in events if event.type == "session.tool.ended")
        assert ended.data["tool_call_id"] == "shell-1-80-24"
        assert ended.data["result"]["output"] == output
        assert ended.data["iteration"] == 1
        for event in events:
            assert mapping_only not in event.model_dump_json()
            assert str(cwd) not in event.model_dump_json()
            if not echo_secret:
                assert secret not in event.model_dump_json()
            assert event.id.startswith("evt_")
            datetime.fromisoformat(event.time)
        admission = next(
            event for event in events if event.type == "session.prompt.admitted"
        )
        assert admission.data["inputID"] == input_id
        assert admission.data["prompt"]["text"] == "print 1 80 24"
        # Inspect while WAL is live, before shutdown checkpoints it away.
        with sqlite3.connect(database) as connection:
            dump = "\n".join(connection.iterdump())
            assert json.dumps(env) not in dump
            assert mapping_only not in dump
            assert str(cwd) not in dump
            with sqlite3.connect(tmp_path / "snapshot.db") as backup:
                connection.backup(backup)
        for path in database.parent.glob("yoke.db*"):
            assert mapping_only.encode() not in path.read_bytes()
            if not echo_secret:
                assert secret.encode() not in path.read_bytes()
        if echo_secret:
            trace = client.get(
                "/api/v1/session/one/tool-call/shell-1-80-24", headers=_auth()
            )
            assert secret not in trace.text
            assert sentinel in trace.text
            # Also cover the inspector's persisted reconstruction path.
            runtime = first_app.state.runtime_registry.get_if_loaded("one")
            runtime.tool_traces = ToolTraceStore()
            trace = client.get(
                "/api/v1/session/one/tool-call/shell-1-80-24", headers=_auth()
            )
            assert secret not in trace.text
            assert sentinel in trace.text
        prompt(client, "one", "terminal", "projection probe")
        wait(client, "one")
        live_projection = requests[-1]
    database.unlink()
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    database.write_bytes((tmp_path / "snapshot.db").read_bytes())
    with TestClient(app()) as client:
        attach(client)
        prompt(client, "one", "terminal", "projection probe")
        wait(client, "one")
        assert requests[-1] == live_projection
