from __future__ import annotations
from tests.yoke.http.fake_session_agent import FakeSessionAgent

# ruff: noqa: D100,D101,D102,D103,S101
import logging
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionRecord

TOKEN = "runtime-lifetime-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _create_session(client: TestClient, root: Path, session_id: str) -> None:
    response = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={
            "id": session_id,
            "title": "Named session",
            "location": {"directory": str(root)},
        },
    )
    assert response.status_code == 200


class CustomAgent(FakeSessionAgent):
    supports_user_message = True
    supports_message_history = False

    def __init__(
        self,
        record: SessionRecord,
        close_counts: dict[str, int],
        *,
        fail: bool = False,
    ) -> None:
        super().__init__(record)
        self.record = record
        self.close_counts = close_counts
        self.fail = fail

    def run(
        self, prompt: str, *, user_message: Message | None = None, **_kwargs
    ) -> AgentResult:
        if self.fail:
            raise RuntimeError("agent failed")
        tree = SessionTree.restore(
            self.record.conversation_entries,
            self.record.leaf_id,
        )
        tree.append_message(user_message or Message.user(prompt))
        tree.append_message(Message.assistant(f"done:{prompt}"))
        return AgentResult(
            output=f"done:{prompt}",
            messages=[],
            iterations=1,
            conversation_entries=list(tree.entries),
        )

    def close(self) -> None:
        if self.fake_is_turn:
            self.close_counts[self.record.id] += 1
        super().close()


@pytest.mark.parametrize(("fail", "expected_state"), [(False, "idle"), (True, "error")])
def test_completed_and_failed_custom_agents_are_closed_once(
    tmp_path: Path,
    fail: bool,
    expected_state: str,
) -> None:
    close_counts: dict[str, int] = defaultdict(int)

    def factory(record: SessionRecord) -> CustomAgent:
        return CustomAgent(record, close_counts, fail=fail)

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=factory,
            indexed_runtime_seed=False,
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(app) as client:
        _create_session(client, root, "session-a")
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "run"}, "resume": True},
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == expected_state
        assert close_counts["session-a"] == 1
    assert close_counts["session-a"] == 1


class CloseCountingProvider:
    supports_image_inputs = True

    def __init__(self) -> None:
        self.close_count = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del tools
        prompt = (messages[-1].plain_text_content or "") if messages else ""
        if prompt.startswith("Create a concise title"):
            return Message.assistant("Generated title")
        return Message.assistant("done")

    def close(self) -> None:
        self.close_count += 1


def test_persistence_and_failure_journal_errors_leave_stable_recoverable_state(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    close_counts: dict[str, int] = defaultdict(int)

    def factory(record: SessionRecord) -> CustomAgent:
        return CustomAgent(record, close_counts)

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=factory,
            indexed_runtime_seed=False,
            max_active_sessions=1,
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        _create_session(client, root, "session-a")
        _create_session(client, root, "session-b")
        runtime = app.state.runtime_registry.get_or_start("session-a")

        def fail_persistence(*_args, **_kwargs) -> None:
            raise OSError("turn persistence failed")

        monkeypatch.setattr(
            runtime.persistence, "persist_turn_entries", fail_persistence
        )
        original_durable = runtime.events.durable

        def fail_failure_event(session_id, event_type, *args, **kwargs):
            if event_type == "session.runtime.failed":
                raise OSError("journal failed")
            return original_durable(session_id, event_type, *args, **kwargs)

        monkeypatch.setattr(runtime.events, "durable", fail_failure_event)
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={
                "id": "uncertain-input",
                "prompt": {"text": "run"},
                "resume": True,
            },
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == "error"
        assert runtime.activity.active is None
        admission = app.state.pending_input_service.admissions.load(
            "session-a"
        ).records["uncertain-input"]
        assert admission.state == "promoted"
        assert admission.settled is False

        second = client.post(
            "/api/v1/session/session-b/prompt",
            headers=_auth(),
            json={"prompt": {"text": "second"}, "resume": True},
        )
        assert second.status_code == 200
        second_wait = client.post(
            "/api/v1/session/session-b/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert second_wait.json()["data"]["state"] == "idle"
    assert close_counts["session-a"] == 1
    assert "HTTP runtime finalization failed" in caplog.text
    assert "Failed to journal session.runtime.failed" in caplog.text


def test_lifespan_closes_broker_then_runtimes_then_index_and_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    order: list[str] = []
    broker = app.state.event_broker
    registry = app.state.runtime_registry
    sessions = app.state.session_service
    original_broker_close = broker.close
    original_runtime_close = registry.close_runtimes
    original_session_close = sessions.close
    original_executor_close = registry.shutdown_executor

    def close_broker() -> None:
        order.append("broker")
        original_broker_close()

    async def close_runtimes(*, grace_seconds: float = 5.0) -> None:
        order.append("runtimes")
        await original_runtime_close(grace_seconds=grace_seconds)

    def close_sessions() -> None:
        order.append("message-index")
        original_session_close()

    def close_executor() -> None:
        order.append("executor")
        original_executor_close()

    monkeypatch.setattr(broker, "close", close_broker)
    monkeypatch.setattr(registry, "close_runtimes", close_runtimes)
    monkeypatch.setattr(sessions, "close", close_sessions)
    monkeypatch.setattr(registry, "shutdown_executor", close_executor)

    with TestClient(app):
        pass

    assert order == ["broker", "runtimes", "message-index", "executor"]
