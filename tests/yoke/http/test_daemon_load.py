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

import httpx
import pytest
from fastapi.testclient import TestClient

from yoke.http.app import HttpAppSettings, create_app

from tests.yoke.http.daemon_load_support import (
    API,
    TOKEN,
    Controller,
    LiveServer,
    RawSseReader,
    auth,
    create_session,
    message_texts,
    percentiles,
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


def test_interrupt_and_steer_spam_against_hanging_tool_stays_responsive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    controller = Controller()
    interrupt_latency: list[float] = []
    steer_latency: list[float] = []
    patch_latency: list[float] = []
    with TestClient(_app(tmp_path, controller)) as client:
        create_session(client, _root(tmp_path), "spam")
        try:
            for index in range(50):
                key = f"hang-{index}"
                # Odd iterations steer into a running turn and retire it; even
                # ones start from idle. Either way the tool ignores cancellation.
                _, elapsed = timed(
                    lambda key=key: prompt(client, "spam", f"hang:{key}")
                )
                steer_latency.append(elapsed)
                assert controller.started[key].wait(5), f"turn {index} never started"
                if index % 2 == 0:
                    continue

                def interrupt_until_idle() -> str:
                    response = client.post(
                        f"{API}/session/spam/interrupt", headers=auth()
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["interrupted"] is True
                    return wait_state(client, "spam", timeout_ms=5000)

                state, elapsed = timed(interrupt_until_idle)
                assert state == "idle"
                interrupt_latency.append(elapsed)

                if index % 10 == 1:
                    # Metadata persistence while detached tool threads are stuck.
                    response, elapsed = timed(
                        lambda index=index: client.patch(
                            f"{API}/session/spam",
                            headers=auth(),
                            json={"title": f"spam {index}"},
                        )
                    )
                    assert response.status_code == 200, response.text
                    patch_latency.append(elapsed)
            # Every steered and interrupted tool is still blocked right now.
            assert len(controller.running) == 50
        finally:
            for event in list(controller.release.values()):
                event.set()

        # Hung tools are detached, not leaked into later turns.
        prompt(client, "spam", "final")
        assert wait_state(client, "spam") == "idle"
        texts = message_texts(client, "spam")
        assert texts[-1] == "done:final"
        assert not any(text.startswith("done:hang:") for text in texts)
        assert (
            client.get(f"{API}/session/spam", headers=auth()).json()["data"]["title"]
            == "spam 41"
        )

    assert max(interrupt_latency) < 2.0, interrupt_latency
    assert max(steer_latency) < 2.0, steer_latency
    assert max(patch_latency) < 1.0, patch_latency
    with capsys.disabled():
        p50, p95 = percentiles(interrupt_latency)
        s50, s95 = percentiles(steer_latency)
        m50, m95 = percentiles(patch_latency)
        print(
            f"\n[daemon-load] interrupt->idle p50={p50:.1f}ms p95={p95:.1f}ms "
            f"max={max(interrupt_latency) * 1000:.1f}ms n={len(interrupt_latency)}"
            f"\n[daemon-load] steer admission p50={s50:.1f}ms p95={s95:.1f}ms"
            f"\n[daemon-load] metadata PATCH p50={m50:.1f}ms p95={m95:.1f}ms"
            f" max={max(patch_latency) * 1000:.1f}ms"
        )


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


def test_slow_sse_consumer_recovers_every_durable_event_through_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    controller = Controller()
    app = _app(tmp_path, controller)
    # A small live queue keeps the overflow cheap; the resync contract is the
    # same for the production default of 512 queued events.
    app.state.event_broker.queue_size = 4
    title = "x" * 64_000
    with (
        LiveServer(app) as server,
        httpx.Client(
            base_url=f"http://127.0.0.1:{server.port}", headers=auth(), timeout=10
        ) as http,
    ):
        root = _root(tmp_path)
        slow = RawSseReader(server.port)
        try:
            first = slow.next_event()
            assert first is not None and first["type"] == "server.connected"
            assert (
                http.post(
                    f"{API}/session",
                    json={
                        "id": "sse",
                        "title": "sse",
                        "location": {"directory": str(root)},
                    },
                ).status_code
                == 200
            )
            # The consumer stops reading while durable events keep arriving,
            # until its socket buffers fill and the broker gives up on it.
            patches = 0
            while not _slow_subscriber_dropped(app) and patches < 200:
                patches += 1
                response = http.patch(
                    f"{API}/session/sse", json={"title": f"{patches} {title}"}
                )
                assert response.status_code == 200, response.text
            # Events published after the drop must come back through history too.
            for _ in range(3):
                patches += 1
                response = http.patch(
                    f"{API}/session/sse", json={"title": f"{patches} {title}"}
                )
                assert response.status_code == 200, response.text
            live: list[dict] = []
            while (event := slow.next_event()) is not None:
                live.append(event)
        finally:
            slow.close()

        types = [event["type"] for event in live]
        assert types[-1] == "server.resyncRequired", types[-5:]
        seen = [
            event["durable"]["seq"]
            for event in live
            if event.get("durable") and event.get("sessionID") == "sse"
        ]
        assert seen == sorted(seen) and len(seen) == len(set(seen))
        assert seen == list(range(seen[0], seen[0] + len(seen)))

        # Client protocol: reconnect live first, then page history after the
        # last delivered sequence, then deduplicate by sequence.
        with http.stream("GET", f"{API}/event") as stream:
            lines = stream.iter_lines()
            assert "server.connected" in next(
                line for line in lines if line.startswith("data: ")
            )
            recovered: list[dict] = []
            after = seen[-1] if seen else 0
            while True:
                page = http.get(
                    f"{API}/session/sse/history", params={"after": after, "limit": 200}
                ).json()
                recovered.extend(page["data"])
                if page["data"]:
                    after = page["data"][-1]["durable"]["seq"]
                if not page["hasMore"]:
                    break
            assert (
                http.patch(
                    f"{API}/session/sse", json={"title": "after resync"}
                ).status_code
                == 200
            )
            tail = None
            for line in lines:
                if not line.startswith("data: "):
                    continue
                payload = httpx.Response(200, content=line[6:]).json()
                if payload.get("durable") and payload.get("sessionID") == "sse":
                    tail = payload
                    break

        assert tail is not None and tail["data"]["title"] == "after resync"
        history_seqs = [event["durable"]["seq"] for event in recovered]
        combined = [*seen, *history_seqs, tail["durable"]["seq"]]
        # Gap-free, duplicate-free and complete from creation to the newest event.
        assert combined == list(range(1, len(combined) + 1)), combined
        titles = [
            event["data"].get("title")
            for event in (*live, *recovered)
            if event.get("type") == "session.updated"
        ]
        assert [t.split(" ", 1)[0] for t in titles] == [
            str(n) for n in range(1, patches + 1)
        ]
    with capsys.disabled():
        print(
            f"\n[daemon-load] slow SSE: {patches} patches, {len(seen)} durable events "
            f"live before resync, {len(history_seqs)} recovered from /history"
        )
