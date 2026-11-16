"""HTTP daemon behavior under concurrent sessions, interrupt spam and races.

Every test drives the public HTTP API of ``create_app`` with real
``RuntimeAgent`` turns (see ``daemon_load_support``). Synchronization uses
events and the ``/wait`` endpoint; timing assertions are generous upper bounds.
"""

# ruff: noqa: D103, S101
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yoke.http.app import HttpAppSettings, create_app

from tests.yoke.http.daemon_load_support import (
    API,
    TOKEN,
    Controller,
    auth,
    create_session,
    message_texts,
    prompt,
    timed,
    wait_state,
)


def _app(
    tmp_path: Path,
    controller: Controller,
    *,
    max_active_sessions: int = 4,
    max_worker_threads: int | None = None,
):
    return create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
            indexed_runtime_seed=False,
            max_active_sessions=max_active_sessions,
            max_worker_threads=max_worker_threads,
        )
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_sessions_beyond_active_limit_queue_and_all_settle(tmp_path: Path) -> None:
    controller = Controller()
    sessions = [f"s{index}" for index in range(6)]
    with TestClient(_app(tmp_path, controller, max_active_sessions=2)) as client:
        root = _root(tmp_path)
        for session_id in sessions:
            create_session(client, root, session_id)
        for session_id in sessions:
            prompt(client, session_id, f"block:{session_id}")

        # Two slots: two turns reach their tool while four are admitted but queued.
        assert controller.started[sessions[0]].wait(5)
        assert controller.started[sessions[1]].wait(5)
        assert controller.max_parallel <= 2

        # Release in reverse admission order so queued sessions start as slots
        # free up, and every session must still settle without deadlocking.
        for session_id in reversed(sessions):
            controller.release[session_id].set()
        states = {session_id: wait_state(client, session_id) for session_id in sessions}

        assert states == dict.fromkeys(sessions, "idle")
        assert controller.max_parallel == 2
        assert sorted(controller.tool_runs) == sessions
        for session_id in sessions:
            assert f"done:block:{session_id}" in message_texts(client, session_id)


@pytest.mark.parametrize(
    "pool",
    [
        pytest.param({}, id="default-pools"),
        pytest.param(
            {"max_active_sessions": 1, "max_worker_threads": 1},
            id="turn-pool-equals-slots",
        ),
    ],
)
def test_control_plane_responds_while_retired_worker_is_stuck(
    tmp_path: Path, pool: dict[str, int]
) -> None:
    """A worker that ignores stop keeps its thread; storage must not wait on it."""
    controller = Controller()
    with TestClient(_app(tmp_path, controller, **pool)) as client:
        create_session(client, _root(tmp_path), "stuck")
        create_session(client, tmp_path / "repo", "other")
        calls = ThreadPoolExecutor(max_workers=1)
        try:
            prompt(client, "stuck", "stall:stuck")
            assert controller.started["stuck"].wait(5)
            interrupted = client.post(f"{API}/session/stuck/interrupt", headers=auth())
            assert interrupted.json()["data"]["interrupted"] is True
            assert wait_state(client, "stuck") == "idle"
            assert "stuck" in controller.running

            def bounded(action):
                # A starved request would block forever; bound it from outside.
                return calls.submit(timed, action).result(timeout=3)

            response, patch_elapsed = bounded(
                lambda: client.patch(
                    f"{API}/session/stuck",
                    headers=auth(),
                    json={"title": "renamed", "pinned": True},
                )
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["title"] == "renamed"
            receipt, admit_elapsed = bounded(
                lambda: prompt(
                    client, "other", "queued", delivery="queue", resume=False
                )
            )
            assert receipt["state"] == "admitted"
            assert patch_elapsed < 1.0
            assert admit_elapsed < 1.0
            assert "stuck" in controller.running
        finally:
            controller.release["stuck"].set()
            calls.shutdown(wait=True)


def test_concurrent_queue_patches_have_exactly_one_winner(tmp_path: Path) -> None:
    controller = Controller()
    with TestClient(_app(tmp_path, controller)) as client:
        create_session(client, _root(tmp_path), "queue")
        for name in ("first", "second"):
            prompt(client, "queue", name, delivery="queue", resume=False)

        def get_queue() -> dict:
            response = client.get(f"{API}/session/queue/queue", headers=auth())
            assert response.status_code == 200, response.text
            return response.json()["data"]

        def patch(revision: int, operations: list[dict[str, object]]):
            return client.patch(
                f"{API}/session/queue/queue",
                headers=auth(),
                json={"expectedRevision": revision, "operations": operations},
            )

        queue = get_queue()
        first, second = (item["id"] for item in queue["items"])
        # Paused items stay queued, so a winner's wake cannot promote them.
        paused = patch(
            queue["revision"],
            [
                {"op": "setPaused", "id": first, "paused": True},
                {"op": "setPaused", "id": second, "paused": True},
            ],
        )
        assert paused.status_code == 200, paused.text

        for round_number in range(10):
            revision = get_queue()["revision"]
            barrier = threading.Barrier(2)

            def edit(item_id: str, text: str, revision: int = revision):
                barrier.wait(5)
                return patch(
                    revision,
                    [{"op": "update", "id": item_id, "prompt": {"text": text}}],
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    item_id: pool.submit(edit, item_id, f"{item_id}-{round_number}")
                    for item_id in (first, second)
                }
                responses = {key: future.result(10) for key, future in futures.items()}

            codes = sorted(response.status_code for response in responses.values())
            assert codes == [200, 409], {k: r.text for k, r in responses.items()}
            loser = next(r for r in responses.values() if r.status_code == 409)
            assert loser.json()["error"]["code"] == "queue_revision_conflict"
            winner = next(k for k, r in responses.items() if r.status_code == 200)

            final = get_queue()
            assert final["revision"] == revision + 1
            assert responses[winner].json()["data"] == final
            texts = {item["id"]: item["prompt"]["text"] for item in final["items"]}
            # The winner's edit is visible and the loser's never lands.
            assert texts[winner] == f"{winner}-{round_number}"
            other = second if winner == first else first
            assert texts[other] != f"{other}-{round_number}"
        assert controller.tool_runs == []
        assert wait_state(client, "queue", timeout_ms=1000) == "idle"


def _slow_subscriber_dropped(app) -> bool:
    # The subscriber stays registered until its blocked generator unwinds, but
    # it is marked closed the moment the broker queues server.resyncRequired.
    broker = app.state.event_broker
    return any(sub.closed for sub in list(broker._subscriptions.values()))
