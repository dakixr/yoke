"""A checkpointed promotion survives loss of its runtime without replaying work."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event

from fastapi.testclient import TestClient

from tests.yoke.loop.test_runtime_request_differential import EchoTool, _calls
from yoke.agent.loop import INTERRUPTED_TURN_NOTICE, RuntimeAgent
from yoke.ai.providers.base import ProviderError
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.models.prompt import PromptAdmissionRequest, PromptInput
from yoke.http.runtime.turns import TurnExecution
from yoke.http.runtime.workers import WorkerTracker
from yoke.session.admissions import INPUT_ID_METADATA_KEY


def test_checkpointed_turn_recovers_once_without_provider_replay(tmp_path):
    calls = []

    class Provider:
        supports_image_inputs = False

        def complete(self, messages, tools):
            calls.append(messages)
            if len(calls) == 1:
                return _calls("checkpoint")
            raise ProviderError("lost process after tool checkpoint")

    settings = HttpAppSettings(
        auth_token=None,
        session_directory=tmp_path / "store",
        agent_factory=lambda _record: RuntimeAgent(
            provider=Provider(), tools=[EchoTool.bind(root=tmp_path)]
        ),
    )
    app = create_app(settings)
    store = app.state.session_service.store
    store.save("crash", [], root=tmp_path, title="Crash fixture")
    pending = app.state.pending_input_service
    request = PromptAdmissionRequest(
        id="once", prompt=PromptInput(text="do the work"), resume=False
    )
    pending.admit("crash", request)
    registry = app.state.runtime_registry
    runtime = registry.get_or_start("crash")

    async def execute_without_settlement():
        reserved = await runtime.reserve_input()
        assert reserved is not None
        execution = TurnExecution(
            turn_id=1,
            admission=reserved.admission,
            started_at="2000-01-01",
            started_monotonic=0,
            stop_event=Event(),
            retired_event=Event(),
            cold_start=True,
            automatic_title=False,
            workspace_use=reserved.workspace_use,
        )
        outcome = None
        try:
            outcome = await asyncio.to_thread(
                runtime.execution.execute, execution, asyncio.get_running_loop()
            )
            assert isinstance(outcome.error, ProviderError)
        finally:
            # Drop resources without ever invoking finalization or settlement.
            if outcome is not None:
                await runtime.resources.reap(outcome.agent)
            await runtime.resources.close()
            reserved.workspace_use.close()

    asyncio.run(execute_without_settlement())
    assert len(calls) == 2
    unsettled = pending.unsettled_promoted("crash")
    assert unsettled is not None and unsettled.id == "once"
    checkpoint = store.load("crash")
    assert any(
        e.metadata.get(INPUT_ID_METADATA_KEY) == "once"
        for e in checkpoint.conversation_entries
    )
    assert any(e.kind == "tool_result" for e in checkpoint.conversation_entries)
    registry.shutdown_executor()
    store.database.close()

    # Re-attach with the same idempotent input twice, including another reopen.
    for _ in range(2):
        reopened = create_app(settings)
        with TestClient(reopened) as client:
            response = client.post(
                "/api/v1/session/crash/prompt",
                json={
                    "id": "once",
                    "prompt": {"text": "do the work"},
                    "resume": True,
                },
            )
            assert response.status_code == 200, response.text
            restored = reopened.state.session_service.store.load("crash")
            assert (
                sum(
                    m.plain_text_content == INTERRUPTED_TURN_NOTICE
                    for m in restored.messages
                )
                == 1
            )
            admission = reopened.state.pending_input_service.admissions.load(
                "crash"
            ).records["once"]
            assert admission.settled and admission.outcome == "recovered"
            assert (
                reopened.state.pending_input_service.unsettled_promoted("crash") is None
            )
            assert len(calls) == 2
        reopened.state.session_service.store.database.close()


def test_worker_drain_waits_for_retained_cleanup_without_cancelling_it():
    async def scenario():
        tracker = WorkerTracker()
        completion: Future[None] = Future()
        tracker.admit(1)
        tracker.retain(1, completion)
        tracker.release(1)
        assert tracker.active_count == 1
        assert await tracker.drain(0) == 1
        assert not completion.cancelled()
        draining = asyncio.create_task(tracker.drain(1))
        await asyncio.sleep(0)
        assert not draining.done()
        completion.set_result(None)
        assert await draining == 0
        assert tracker.active_count == 0

    asyncio.run(scenario())
