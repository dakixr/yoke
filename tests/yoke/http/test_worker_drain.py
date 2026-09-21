from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

import asyncio
from collections import defaultdict
from pathlib import Path
from threading import Event
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationLog
from yoke.agent.models import Message
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.services.session_runtime.execution import TurnExecution
from yoke.session import SessionRecord
from yoke.session.admissions import AdmissionRecord
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    CustomAgent,
    _auth,
    _create_session,
)


def drain(client: TestClient, timeout: int = 20) -> dict:
    response = client.post(
        "/api/v1/session/session-a/drain",
        headers=_auth(),
        params={"timeoutMs": timeout},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("cancel_at", [None, "worker", "cleanup"])
@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_drain_tracks_retired_worker_and_cleanup(
    tmp_path: Path, cancel_at: str | None, fail_cleanup: bool
) -> None:
    started, release = Event(), Event()
    cleanup_started, cleanup_release = Event(), Event()
    failed = Event()
    counts: dict[str, int] = defaultdict(int)

    class Agent(CustomAgent):
        def run(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            return super().run(*args, **kwargs)

        def close(self) -> None:
            cleanup_started.set()
            if fail_cleanup and not failed.is_set():
                failed.set()
                raise RuntimeError("retry cleanup")
            assert cleanup_release.wait(5)
            super().close()

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: Agent(record, counts),
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        try:
            response = client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "run"}, "resume": True},
            )
            assert response.status_code == 200
            assert started.wait(2)
            runtime = app.state.runtime_registry.get_if_loaded("session-a")
            task = runtime._active.task
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            stopped = client.post(
                "/api/v1/session/session-a/interrupt", headers=_auth()
            )
            assert stopped.json()["data"]["interrupted"] is True
            waited = client.post("/api/v1/session/session-a/wait", headers=_auth())
            assert waited.json()["data"]["state"] == "idle"

            async def cancel() -> None:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            portal = client.portal
            assert portal is not None
            if cancel_at == "worker":
                portal.call(cancel)
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            release.set()
            assert cleanup_started.wait(2)
            if cancel_at == "cleanup":
                portal.call(cancel)
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            cleanup_release.set()
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
            assert counts["session-a"] == 1
            # A drained retired turn neither blocks nor quarantines the next turn.
            response = client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "next"}, "resume": True},
            )
            assert response.status_code == 200
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
        finally:
            release.set()
            cleanup_release.set()


def test_prestart_interrupt_and_cancelled_drain_observer(tmp_path: Path) -> None:
    def factory(_record: SessionRecord) -> CustomAgent:
        pytest.fail("pre-start interruption must not construct an agent")

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=factory,
            max_active_sessions=1,
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        registry = app.state.runtime_registry
        portal = client.portal
        assert portal is not None
        portal.call(registry.active_slots.acquire)
        try:
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "run"}, "resume": True},
            )
            runtime = registry.get_if_loaded("session-a")
            assert drain(client) == {"drained": False, "activeWorkers": 1}

            async def cancel_observer() -> None:
                observer = asyncio.create_task(runtime.workers.drain(30))
                await asyncio.sleep(0)
                observer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await observer

            portal.call(cancel_observer)
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            client.post("/api/v1/session/session-a/interrupt", headers=_auth())
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
        finally:
            portal.call(registry.active_slots.release)


def test_drain_auth_validation_and_unloaded_session(tmp_path: Path) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        path = "/api/v1/session/session-a/drain"
        assert client.post(path).status_code == 401
        assert drain(client) == {"drained": True, "activeWorkers": 0}
        assert app.state.runtime_registry.get_if_loaded("session-a") is None
        assert (
            client.post("/api/v1/session/missing/drain", headers=_auth()).status_code
            == 404
        )
        for timeout in (0, -1, 300001, "NaN"):
            assert (
                client.post(
                    path, headers=_auth(), params={"timeoutMs": timeout}
                ).status_code
                == 422
            )


def test_retired_first_turn_checkpoint_cannot_replace_interruption_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        runtime = app.state.runtime_registry.get_or_start("session-a")
        admission = AdmissionRecord(
            id="first-turn",
            session_id="session-a",
            prompt="run",
            delivery="queue",
            fingerprint="test-fingerprint",
            time_created="2026-09-19T00:00:00+00:00",
            admitted_seq=1,
        )
        execution = TurnExecution(
            turn_id=1,
            admission=admission,
            started_at="2026-09-19T00:00:00+00:00",
            started_monotonic=0.0,
            stop_event=Event(),
            retired_event=Event(),
            cold_start=True,
            automatic_title=False,
        )
        first = ConversationEntry(kind="user", message=Message.user("run"))
        runtime._persist_turn_entries(execution, [first], input_id=admission.id)
        active = runtime._snapshot().owned_active_path()
        stale = [entry.model_copy(deep=True) for entry in active]
        stale.append(
            ConversationEntry(
                kind="assistant",
                message=Message.assistant("stale worker answer"),
                parent_id=stale[-1].id,
            )
        )
        context = AgentContext(conversation_log=ConversationLog(entries=stale))

        entered, release = Event(), Event()
        original = runtime._persist_turn_entries

        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(5), "test did not release stale checkpoint"
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime, "_persist_turn_entries", delayed)
        worker = Thread(
            target=runtime._checkpoint,
            args=(execution, object(), context),
            daemon=True,
        )
        worker.start()
        assert entered.wait(2)

        execution.retired_event.set()
        runtime._persist_interrupted_checkpoint(admission)
        interrupted = runtime.store.load("session-a")
        assert interrupted is not None
        interrupted_leaf = interrupted.leaf_id
        interrupted_entries = [
            entry.model_dump(mode="json") for entry in interrupted.conversation_entries
        ]
        assert "stale worker answer" not in str(interrupted_entries)

        release.set()
        worker.join(2)
        assert not worker.is_alive()
        saved = runtime.store.load("session-a")
        assert saved is not None
        assert saved.leaf_id == interrupted_leaf
        assert [
            entry.model_dump(mode="json") for entry in saved.conversation_entries
        ] == interrupted_entries


def test_interrupt_before_controller_first_step(tmp_path: Path) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "never start"}, "resume": False},
        )
        registry = app.state.runtime_registry

        async def start_and_interrupt() -> None:
            await registry.wake("session-a")
            runtime = registry.get_if_loaded("session-a")
            execution = runtime._active
            assert not execution.worker_started
            assert await runtime.interrupt() == (True, execution.turn_id)
            assert await runtime.workers.drain(1) == 0
            assert execution.task.cancelled()

        assert client.portal is not None
        client.portal.call(start_and_interrupt)
        assert drain(client) == {"drained": True, "activeWorkers": 0}


def test_retirement_transfer_failure_retries_without_false_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources

    original = SessionRuntimeResources.retire
    failed, allow_retirement = Event(), Event()

    def retire(self, agent):
        if not allow_retirement.is_set():
            failed.set()
            raise RuntimeError("retirement transfer unavailable")
        return original(self, agent)

    monkeypatch.setattr(SessionRuntimeResources, "retire", retire)
    counts: dict[str, int] = defaultdict(int)
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: CustomAgent(record, counts),
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        try:
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "run"}, "resume": True},
            )
            assert failed.wait(2)
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            allow_retirement.set()
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
            assert counts["session-a"] == 1
        finally:
            allow_retirement.set()


def test_multiple_generations_and_logical_wait_stay_independent(tmp_path: Path) -> None:
    gates = {name: (Event(), Event()) for name in ("old", "new")}
    counts: dict[str, int] = defaultdict(int)

    class Agent(CustomAgent):
        def run(self, prompt, **kwargs):
            started, release = gates[prompt]
            started.set()
            assert release.wait(5)
            return super().run(prompt, **kwargs)

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: Agent(record, counts),
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        try:
            for name in ("old", "new"):
                client.post(
                    "/api/v1/session/session-a/prompt",
                    headers=_auth(),
                    json={"prompt": {"text": name}, "resume": True},
                )
                assert gates[name][0].wait(2)
                if name == "old":
                    client.post("/api/v1/session/session-a/interrupt", headers=_auth())
            assert drain(client) == {"drained": False, "activeWorkers": 2}
            gates["new"][1].set()
            waited = client.post(
                "/api/v1/session/session-a/wait",
                headers=_auth(),
                params={"timeoutMs": 1000},
            )
            assert waited.json()["data"]["state"] == "idle"
            assert counts["session-a"] == 1
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            gates["old"][1].set()
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
        finally:
            for _, release in gates.values():
                release.set()


def test_failed_preparation_cleanup_is_owned_until_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources

    cleanup_started, release_cleanup = Event(), Event()
    counts: dict[str, int] = defaultdict(int)

    class Agent(CustomAgent):
        def close(self) -> None:
            cleanup_started.set()
            assert release_cleanup.wait(5)
            super().close()

    original = SessionRuntimeResources.prepare_turn

    def prepare(self, *args, **kwargs):
        agent = original(self, *args, **kwargs)
        self.retire(agent)
        raise RuntimeError("preparation failed after owning an agent")

    monkeypatch.setattr(SessionRuntimeResources, "prepare_turn", prepare)
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda record: Agent(record, counts),
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        try:
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "fail preparation"}, "resume": True},
            )
            assert cleanup_started.wait(2)
            assert drain(client) == {"drained": False, "activeWorkers": 1}
            release_cleanup.set()
            assert drain(client, 3000) == {"drained": True, "activeWorkers": 0}
            assert counts["session-a"] == 1
        finally:
            release_cleanup.set()
