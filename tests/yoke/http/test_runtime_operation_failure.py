from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.session import SessionRecord


TOKEN = "runtime-operation-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _create_session(client: TestClient, root: Path, session_id: str) -> None:
    response = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": session_id, "location": {"directory": str(root)}},
    )
    assert response.status_code == 200
    renamed = client.patch(
        f"/api/v1/session/{session_id}",
        headers=_auth(),
        json={"title": "Named session"},
    )
    assert renamed.status_code == 200


class PromptProvider:
    supports_image_inputs = True

    def __init__(self, prompt_started: Event) -> None:
        self.prompt_started = prompt_started

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        self.prompt_started.set()
        return Message.assistant("prompt completed")


def test_accepted_compaction_failure_is_waitable_and_drains_admitted_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import yoke.http.services.runtime as runtime_module

    compaction_started = Event()
    release_compaction = Event()
    prompt_started = Event()

    def fail_compaction(*_args: object, **_kwargs: object) -> None:
        compaction_started.set()
        if not release_compaction.wait(2):
            raise TimeoutError("compaction gate was not released")
        raise RuntimeError("compaction failed")

    monkeypatch.setattr(runtime_module, "force_compact_agent", fail_compaction)

    def factory(_record: SessionRecord) -> RuntimeAgent:
        return RuntimeAgent(PromptProvider(prompt_started), [])

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
        compact = client.post(
            "/api/v1/session/session-a/compact",
            headers=_auth(),
            json={"reason": "manual"},
        )
        assert compact.status_code == 202
        assert compaction_started.wait(1)

        runtime = app.state.runtime_registry.get_if_loaded("session-a")
        assert runtime is not None
        wait_started = Event()
        original_wait = runtime.wait

        async def marked_wait(timeout_seconds: float | None = None):  # noqa: ANN202
            wait_started.set()
            return await original_wait(timeout_seconds)

        monkeypatch.setattr(runtime, "wait", marked_wait)
        with ThreadPoolExecutor(max_workers=1) as requests:
            waiting = requests.submit(
                client.post,
                "/api/v1/session/session-a/wait",
                headers=_auth(),
                params={"timeoutMs": 3000},
            )
            assert wait_started.wait(1)
            admitted = client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={
                    "id": "after-compaction",
                    "prompt": {"text": "continue after failure"},
                    "resume": True,
                },
            )
            assert admitted.status_code == 200
            release_compaction.set()
            assert prompt_started.wait(1)
            waited = waiting.result(timeout=3)

        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == "idle"
        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        failure = next(
            item for item in history if item["type"] == "session.compaction.failed"
        )
        assert failure["data"]["operationID"] == compact.json()["data"]["operationID"]


def test_request_bound_selection_failure_still_propagates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import yoke.http.services.runtime as runtime_module

    def fail_selection(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("selection failed")

    monkeypatch.setattr(runtime_module, "switch_agent_provider_model", fail_selection)
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(PromptProvider(Event()), []),
        )
    )
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(app) as client:
        _create_session(client, root, "session-a")
        with pytest.raises(RuntimeError, match="selection failed"):
            client.post(
                "/api/v1/session/session-a/selection",
                headers=_auth(),
                json={"provider": "demo", "model": "new-model"},
            )
