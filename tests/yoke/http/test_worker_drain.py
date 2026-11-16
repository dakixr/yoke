from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101
import asyncio
from collections import defaultdict
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    CustomAgent,
    _auth,
    _create_session,
)
from yoke.http.app import HttpAppSettings, create_app


def drain(client: TestClient, timeout: int = 20) -> dict:
    response = client.post(
        "/api/v1/session/session-a/drain",
        headers=_auth(),
        params={"timeoutMs": timeout},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize(
    "cancel_at,fail_cleanup", [(None, False), ("worker", True), ("cleanup", False)]
)
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
            indexed_runtime_seed=False,
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
            task = runtime.activity.active.task
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
