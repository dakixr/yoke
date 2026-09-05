from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
from threading import Event
import time
from typing import Any
from typing import cast

import pytest
from fastapi.testclient import TestClient

from yoke.agent.loop import AgentResult
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
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


class CustomAgent:
    supports_user_message = True

    def __init__(
        self,
        record: SessionRecord,
        close_counts: dict[str, int],
        *,
        fail: bool = False,
    ) -> None:
        self.record = record
        self.close_counts = close_counts
        self.fail = fail

    def run(self, prompt: str, *, user_message: Message, **_kwargs) -> AgentResult:
        if self.fail:
            raise RuntimeError("agent failed")
        tree = SessionTree.restore(
            self.record.conversation_entries,
            self.record.leaf_id,
        )
        tree.append_message(user_message)
        tree.append_message(Message.assistant(f"done:{prompt}"))
        return AgentResult(
            output=f"done:{prompt}",
            messages=[],
            iterations=1,
            conversation_entries=list(tree.entries),
        )

    def close(self) -> None:
        self.close_counts[self.record.id] += 1


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


def test_primary_runtime_provider_closes_once_at_lifespan_exit(tmp_path: Path) -> None:
    provider = CloseCountingProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(app) as client:
        _create_session(client, root, "session-a")
        client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "run"}, "resume": True},
        )
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.json()["data"]["state"] == "idle"
        assert provider.close_count == 0
    assert provider.close_count == 1


def test_shutdown_waits_for_retired_fork_before_stopping_runtime_executor(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    provider_started = Event()
    release_provider = Event()
    resource_close_started = Event()

    class BlockingProvider(CloseCountingProvider):
        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del messages, tools
            provider_started.set()
            if not release_provider.wait(2):
                raise TimeoutError("provider gate was not released")
            return Message.assistant("done")

    provider = BlockingProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    client = TestClient(app)
    client.__enter__()
    try:
        _create_session(client, root, "session-a")
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "run"}, "resume": True},
        )
        assert admitted.status_code == 200
        assert provider_started.wait(1)
        runtime = app.state.runtime_registry.get_if_loaded("session-a")
        assert runtime is not None
        original_close = runtime.resources.close

        async def marked_close() -> None:
            resource_close_started.set()
            await original_close()

        monkeypatch.setattr(runtime.resources, "close", marked_close)
        with (
            caplog.at_level(logging.ERROR),
            ThreadPoolExecutor(max_workers=1) as requests,
        ):
            closing = requests.submit(client.__exit__, None, None, None)
            assert resource_close_started.wait(1)
            assert app.state.runtime_registry._executor_closed is False
            release_provider.set()
            closing.result(timeout=3)
    finally:
        if not app.state.runtime_registry._executor_closed:
            release_provider.set()
            client.__exit__(None, None, None)

    assert provider.close_count == 1
    assert "cannot schedule new futures" not in caplog.text


def test_registry_grace_timeout_does_not_block_event_loop_executor_teardown(
    tmp_path: Path,
) -> None:
    provider_started = Event()
    release_provider = Event()

    class BlockingProvider(CloseCountingProvider):
        def complete(self, messages, tools) -> Message:  # noqa: ANN001
            del messages, tools
            provider_started.set()
            release_provider.wait(5)
            return Message.assistant("done")

    provider = BlockingProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    client = TestClient(app)
    client.__enter__()
    closed = False
    try:
        _create_session(client, root, "session-a")
        client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "run"}, "resume": True},
        )
        assert provider_started.wait(1)
        portal = client.portal
        assert portal is not None

        async def close_with_short_grace() -> None:
            await app.state.runtime_registry.close_runtimes(grace_seconds=0.01)

        portal.call(close_with_short_grace)
        app.state.runtime_registry.shutdown_executor()
        with ThreadPoolExecutor(max_workers=1) as requests:
            closing = requests.submit(client.__exit__, None, None, None)
            closing.result(timeout=1)
            closed = True
        assert release_provider.is_set() is False
    finally:
        release_provider.set()
        if not closed:
            client.__exit__(None, None, None)


def test_controller_task_cancellation_retains_outcome_and_physical_slot(
    tmp_path: Path,
) -> None:
    provider_started = Event()
    release_provider = Event()

    class ForkProvider(CloseCountingProvider):
        def complete(self, messages, tools) -> Message:  # noqa: ANN001
            del messages, tools
            provider_started.set()
            release_provider.wait(5)
            return Message.assistant("late result")

    fork_provider = ForkProvider()

    class PrimaryProvider(CloseCountingProvider):
        def fork_for_turn(self) -> CloseCountingProvider:
            return fork_provider

    primary_provider = PrimaryProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(primary_provider, []),
            max_active_sessions=1,
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(app) as client:
        try:
            _create_session(client, root, "session-a")
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "run"}, "resume": True},
            )
            assert provider_started.wait(1)
            runtime = app.state.runtime_registry.get_if_loaded("session-a")
            assert runtime is not None
            execution = runtime._active
            assert execution is not None
            task = execution.task
            assert task is not None
            portal = client.portal
            assert portal is not None

            async def cancel_controller() -> None:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            portal.call(cancel_controller)
            assert runtime.active_slots.locked()
            assert fork_provider.close_count == 0
            release_provider.set()

            deadline = time.monotonic() + 1
            while (
                fork_provider.close_count == 0 or runtime.active_slots.locked()
            ) and time.monotonic() < deadline:
                time.sleep(0.005)
            assert fork_provider.close_count == 1
            assert not runtime.active_slots.locked()
        finally:
            release_provider.set()


def test_temporary_title_provider_closes_after_regeneration(tmp_path: Path) -> None:
    provider = CloseCountingProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
        )
    )
    store = app.state.session_service.store
    store.save(
        "session-a",
        [Message.user("Title this"), Message.assistant("Done")],
        root=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/session/session-a/title/regenerate",
            headers=_auth(),
            json={},
        )
        assert response.status_code == 200
        assert provider.close_count == 1
    assert provider.close_count == 1


def test_cold_title_load_does_not_block_the_asgi_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    load_started = Event()
    release_load = Event()
    captured_messages: list[list[str]] = []

    class CapturingTitleProvider(CloseCountingProvider):
        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            captured_messages.append(
                [message.plain_text_content or "" for message in messages]
            )
            return super().complete(messages, tools)

    provider = CapturingTitleProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
        )
    )
    store = app.state.session_service.store
    store.save(
        "session-a",
        [
            Message.user("first saved message"),
            Message.assistant("middle saved message"),
            Message.user("last saved message"),
        ],
        root=tmp_path,
    )
    original_load = store.load

    def blocked_load(session_id: str):  # noqa: ANN202
        if session_id == "session-a":
            load_started.set()
            if not release_load.wait(2):
                raise TimeoutError("title load gate was not released")
        return original_load(session_id)

    monkeypatch.setattr(store, "load", blocked_load)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as requests:
        regenerating = requests.submit(
            client.post,
            "/api/v1/session/session-a/title/regenerate",
            headers=_auth(),
            json={},
        )
        assert load_started.wait(1)
        health = client.get("/api/v1/health", headers=_auth())
        assert health.status_code == 200
        release_load.set()
        regenerated = regenerating.result(timeout=3)
        assert regenerated.status_code == 200

    title_request = captured_messages[0]
    assert "first saved message" in title_request
    assert "middle saved message" in title_request
    assert "last saved message" in title_request


def test_agent_close_failure_still_closes_title_provider_and_is_logged(
    tmp_path: Path,
    caplog,
) -> None:
    provider = CloseCountingProvider()

    class BrokenCloseAgent(RuntimeAgent):
        def close(self) -> None:
            super().close()
            raise OSError("agent close failed")

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: BrokenCloseAgent(provider, []),
        )
    )
    store = app.state.session_service.store
    store.save("session-a", [Message.user("Title this")], root=tmp_path)
    with caplog.at_level(logging.ERROR):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/session/session-a/title/regenerate",
                headers=_auth(),
                json={},
            )
            assert response.status_code == 200
    assert provider.close_count == 1
    assert "Failed to close HTTP title agent" in caplog.text


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

        monkeypatch.setattr(runtime, "_persist_turn_entries", fail_persistence)
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
        assert runtime._active is None
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


def test_registry_logs_runtime_close_failure_with_session_identity(
    tmp_path: Path,
    caplog,
) -> None:
    class BrokenRuntime:
        async def close(self) -> None:
            raise OSError("runtime close failed")

    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    registry = app.state.runtime_registry
    registry._runtimes["broken-session"] = cast(Any, BrokenRuntime())
    try:
        with caplog.at_level(logging.ERROR):
            asyncio.run(registry.close_runtimes())
    finally:
        app.state.session_service.close()
        registry.shutdown_executor()

    assert "Failed to close HTTP runtime broken-session" in caplog.text
