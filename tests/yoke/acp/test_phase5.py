"""Input-scoped ACP cancellation and cleanup regressions."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from acp import RequestError

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.state import ActiveTurn
from tests.yoke.acp.test_bridge import FixtureNative


class LifecycleNative(FixtureNative):
    def __init__(self, *, settled: bool = False) -> None:
        super().__init__()
        self.input_lifecycle = True
        self.phase = "settled" if settled else "queued"
        self.running = True  # Another client's turn owns the runtime.
        self.cancel_calls = 0

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/input"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inputRevision": 2,
                        "inputs": [
                            {
                                "id": "owned",
                                "phase": self.phase,
                                "outcome": "completed"
                                if self.phase == "settled"
                                else None,
                            }
                        ],
                    }
                },
            )
        if path.endswith("/input/owned/cancel"):
            self.cancel_calls += 1
            if self.phase == "settled":
                return httpx.Response(409, json={})
            self.phase = "removed"
            return httpx.Response(
                200, json={"data": {"id": "owned", "phase": self.phase}}
            )
        if path.endswith("/input/owned"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "owned",
                        "phase": self.phase,
                        "outcome": "completed" if self.phase == "settled" else None,
                    }
                },
            )
        if path.endswith("/prompt"):
            self.admission = {"id": "owned"}
            self.admitted.set()
            return httpx.Response(
                200,
                json={
                    "data": {"id": "owned", "phase": self.phase, "state": "admitted"}
                },
            )
        if path.endswith("/history"):
            return httpx.Response(200, json={"data": [], "hasMore": False})
        return await super().respond(request)


@pytest.mark.parametrize("settled", [False, True])
def test_cancel_owns_input_and_preserves_settled_answer(settled: bool) -> None:
    async def scenario() -> None:
        native = LifecycleNative(settled=settled)
        work = ActiveTurn(
            True, asyncio.Event(), asyncio.Event(), asyncio.current_task()
        )
        work.wake.set()
        emitted = []

        async def emit(kind, data):
            emitted.append((kind, data))

        try:
            result = await native.turn("native-id", "prompt", "owned", work, emit)
            assert result == ("end_turn" if settled else "cancelled")
            assert native.running, (
                "cancel must leave the other client's runtime running"
            )
            assert not native.interrupted
            assert native.phase == ("settled" if settled else "removed")
            assert any(kind == "answer" for kind, _ in emitted) == settled
        finally:
            await native.close()

    asyncio.run(scenario())


def test_cancel_during_final_drain_cannot_hide_answer(monkeypatch) -> None:
    async def scenario() -> None:
        native = FixtureNative()
        work = ActiveTurn(
            False, asyncio.Event(), asyncio.Event(), asyncio.current_task()
        )
        emitted = []
        original = native.drained

        async def drained(session_id, timeout_ms=30_000):
            await original(session_id, timeout_ms)
            if native.admission is not None:
                work.cancel = True

        monkeypatch.setattr(native, "drained", drained)

        async def emit(kind, data):
            emitted.append(kind)

        try:
            assert (
                await native.turn("native-id", "prompt", "owned", work, emit)
                == "end_turn"
            )
            assert "answer" in emitted
        finally:
            await native.close()

    asyncio.run(scenario())


def test_close_cleans_local_session_even_when_detach_fails(monkeypatch) -> None:
    async def scenario() -> None:
        native = FixtureNative()
        agent = YokeAcpAgent(native)
        await agent.new_session("/tmp")
        agent.lifecycle.configs["native-id"] = {}
        original = native.data

        async def data(method, path, **kwargs):
            if method == "DELETE":
                assert "native-id" in agent.busy, (
                    "close must serialize session mutations"
                )
                raise RequestError(-32001, "detach failed")
            return await original(method, path, **kwargs)

        monkeypatch.setattr(native, "data", data)
        try:
            with pytest.raises(RequestError, match="detach failed"):
                await agent.close_session("native-id")
            assert "native-id" not in agent.sessions
            assert "native-id" not in agent.catalogs
            assert "native-id" not in agent.lifecycle.configs
            assert "native-id" not in agent.busy
        finally:
            agent.lifecycle.configs.clear()
            await agent.close()

    asyncio.run(scenario())


def test_cancel_conflict_reads_the_settled_input() -> None:
    async def scenario() -> None:
        native = LifecycleNative(settled=True)
        try:
            result = await native.cancel_input("native-id", "owned")
            assert result["phase"] == "settled"
            assert result["outcome"] == "completed"
            assert native.cancel_calls == 1
            assert native.running
        finally:
            await native.close()

    asyncio.run(scenario())


def test_real_daemon_cancel_removes_acp_queue_without_stopping_other_client(
    tmp_path,
    monkeypatch,
) -> None:
    from tests.yoke.acp.test_native_input import ReceiptNative
    from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for

    async def scenario() -> None:
        provider = RecordingProvider()
        provider.release.clear()
        app = app_for(tmp_path, provider)
        async with app.router.lifespan_context(app):
            native = ReceiptNative("http://fixture", "unused")
            await native.http.aclose()
            native.http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture/api/v1/",
                headers={"Authorization": "Bearer runtime-lifetime-secret"},
            )
            native.input_lifecycle = True
            admitted = asyncio.Event()
            original = native.data

            async def data(method, path, **kwargs):
                result = await original(method, path, **kwargs)
                if (
                    path.endswith("/prompt")
                    and kwargs.get("json", {}).get("id") == "owned"
                ):
                    admitted.set()
                return result

            monkeypatch.setattr(native, "data", data)
            work = ActiveTurn(
                False, asyncio.Event(), asyncio.Event(), asyncio.current_task()
            )

            async def emit(_kind, _data):
                pass

            prompt = None
            try:
                await native.data(
                    "POST",
                    "session",
                    json={
                        "id": "shared",
                        "title": "Shared cancellation",
                        "location": {"directory": str(tmp_path)},
                    },
                )
                await native.data(
                    "POST",
                    "session/shared/prompt",
                    json={
                        "id": "other",
                        "prompt": {"text": "other client"},
                        "delivery": "queue",
                    },
                )
                assert await asyncio.to_thread(provider.started.wait, 3)
                prompt = asyncio.create_task(
                    native.turn("shared", "ACP queued", "owned", work, emit)
                )
                await asyncio.wait_for(admitted.wait(), 3)
                assert (await native.data("GET", "session/shared/input/owned"))[
                    "phase"
                ] == "queued"
                work.cancel = True
                work.wake.set()
                assert await asyncio.wait_for(prompt, 3) == "cancelled"
                assert (await native.data("GET", "session/shared/input/owned"))[
                    "phase"
                ] == "removed"
                assert (await native.data("GET", "session/shared/input/other"))[
                    "phase"
                ] == "running"
                assert not native.interrupted.is_set()
                assert len(provider.calls) == 1
            finally:
                provider.release.set()
                if prompt is not None:
                    await asyncio.gather(prompt, return_exceptions=True)
                await native.data("POST", "session/shared/wait?timeoutMs=3000")
                await native.drained("shared")
                await native.close()

    asyncio.run(scenario())


def test_input_snapshot_fences_older_paused_history() -> None:
    class SnapshotRaceNative(LifecycleNative):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/input"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "inputRevision": 2,
                            "inputs": [
                                {"id": "owned", "phase": "running"},
                            ],
                        }
                    },
                )
            if path.endswith("/history") and self.admission is not None:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "session.input.changed",
                                "durable": {"seq": revision},
                                "data": {
                                    "inputID": "owned",
                                    "inputRevision": revision,
                                    "phase": phase,
                                    "pauseCause": "workspace",
                                    "outcome": "completed",
                                },
                            }
                            for revision, phase in [(1, "paused"), (3, "settled")]
                        ],
                        "hasMore": False,
                    },
                )
            return await super().respond(request)

    async def scenario() -> None:
        native = SnapshotRaceNative()
        native.phase = "running"
        work = ActiveTurn(
            False, asyncio.Event(), asyncio.Event(), asyncio.current_task()
        )

        async def emit(_kind, _data):
            pass

        try:
            assert (
                await native.turn("native-id", "prompt", "owned", work, emit)
                == "end_turn"
            )
            assert work.phase == "settled"
        finally:
            await native.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_input_lifecycle_uses_advertised_capability_after_attach(enabled: bool) -> None:
    class CapabilityNative(FixtureNative):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/openapi.json"):
                return httpx.Response(
                    200,
                    json={
                        "paths": {
                            "/api/v1/session/{session_id}/input": {"get": {}},
                            "/api/v1/session/{session_id}/client": {"put": {}},
                        }
                    },
                )
            if request.url.path.endswith("/capabilities"):
                return httpx.Response(
                    200, json={"data": {"features": {"inputLifecycle": enabled}}}
                )
            if request.url.path.endswith("/client"):
                return httpx.Response(200, json={"data": {}})
            return await super().respond(request)

    async def scenario() -> None:
        native = CapabilityNative()
        agent = YokeAcpAgent(native)
        try:
            await agent.initialize(1)
            assert not native.input_lifecycle
            await agent.lifecycle.configure("native-id", "/tmp", [], [])
            assert native.input_lifecycle is enabled
        finally:
            await agent.close()

    asyncio.run(scenario())
