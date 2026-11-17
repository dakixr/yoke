"""Failures at runtime ownership boundaries must leave a waitable session."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.yoke.http.test_runtime_ownership import _execution, _runtime
from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.session.events import SessionEventJournal


def test_b6_failed_stop_write_clears_active_and_publishes_error(tmp_path, monkeypatch):
    registry, runtime = _runtime(tmp_path)
    published = []
    monkeypatch.setattr(
        runtime.events.broker, "publish", lambda event, **kw: published.append(event)
    )

    def fail(*args, **kwargs):
        raise OSError("stop write failed")

    monkeypatch.setattr(runtime.persistence, "persist_interruption", fail)

    async def scenario():
        execution = _execution()
        runtime.activity.active = execution
        with pytest.raises(OSError, match="stop write failed"):
            await runtime.interrupt()
        assert execution.retired_event.is_set()
        assert runtime.activity.active is None
        status = await runtime.wait(0.2)
        assert status.state == "error"
        assert "stop write failed" in (status.last_error or "")
        assert published[-1].type == "session.active.changed"
        assert published[-1].data["state"] == "error"

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_b7_failed_compaction_start_journal_leaves_idle(tmp_path, monkeypatch):
    registry, runtime = _runtime(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("start journal failed")

    monkeypatch.setattr(runtime.events, "durable", fail)

    async def scenario():
        with pytest.raises(OSError, match="start journal failed"):
            await runtime.compact()
        assert runtime.activity.operation is None
        assert (await runtime.wait(0.2)).state == "idle"

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


@pytest.mark.parametrize(
    "event_type",
    [
        "session.active.changed",
        "session.permission.requested",
        "session.permission.resolved",
        "session.question.requested",
        "session.question.resolved",
    ],
)
def test_b9_state_event_overflow_requests_resync(tmp_path: Path, event_type: str):
    async def scenario():
        broker = GlobalEventBroker(queue_size=1)
        events = EventService(SessionEventJournal(tmp_path / "events"), broker)
        subscription = broker.subscribe()
        events.live("session.tool.delta", {})
        events.live(event_type, {})
        await asyncio.sleep(0)
        assert subscription.closed
        event = await subscription.queue.get()
        assert event is not None and event.type == "server.resyncRequired"
        broker.close()

    asyncio.run(scenario())


def test_b10_selection_reports_running_while_waiting_for_slot(tmp_path):
    registry, runtime = _runtime(tmp_path)

    async def scenario():
        # No worker can run. Only the operation's publication is under test.
        runtime.operations.active_slots = asyncio.Semaphore(0)
        task = asyncio.create_task(
            runtime.select_model(
                provider_name="demo", model_id="new", reasoning_effort=None
            )
        )
        while runtime.activity.operation is None:
            await asyncio.sleep(0)
        try:
            assert (await runtime.status()).state == "running"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_cancelled_compaction_waiting_for_slot_clears_busy(tmp_path):
    registry, runtime = _runtime(tmp_path)

    async def scenario():
        runtime.operations.active_slots = asyncio.Semaphore(0)
        await runtime.compact()
        operation = runtime.activity.operation
        assert operation is not None and operation.task is not None
        await asyncio.sleep(0)
        operation.task.cancel()
        await asyncio.gather(operation.task, return_exceptions=True)
        assert runtime.activity.operation is None
        assert (await runtime.wait(0.2)).state == "error"

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_compaction_cancelled_before_first_poll_still_settles(tmp_path):
    registry, runtime = _runtime(tmp_path)

    async def scenario():
        await runtime.compact()
        operation = runtime.activity.operation
        assert operation is not None and operation.task is not None
        operation.task.cancel()
        await asyncio.gather(operation.task, return_exceptions=True)
        assert (await runtime.wait(0.2)).state == "error"
        assert runtime.activity.operation is None

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_compaction_cancellation_retains_running_worker_lane(tmp_path, monkeypatch):
    from threading import Event

    registry, runtime = _runtime(tmp_path)
    started, release = Event(), Event()

    def compact():
        started.set()
        assert release.wait(2)
        return {"compacted": True}

    monkeypatch.setattr(runtime.operations, "_compact_sync", compact)

    async def scenario():
        runtime.operations.active_slots = asyncio.Semaphore(1)
        await runtime.compact()
        operation = runtime.activity.operation
        assert operation is not None and operation.task is not None
        assert await asyncio.to_thread(started.wait, 1)
        try:
            result = await runtime.interrupt_result()
            assert result.interrupted is False
            assert result.reason == "operation_not_interruptible"
            operation.task.cancel()
            await asyncio.sleep(0)
            assert runtime.activity.operation is operation
            assert runtime.operations.active_slots.locked()
            assert not operation.task.done()
        finally:
            release.set()
        await asyncio.gather(operation.task, return_exceptions=True)
        assert (await runtime.wait(0.2)).state == "error"
        assert not runtime.operations.active_slots.locked()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        registry.shutdown_executor()


def test_permission_and_question_waits_publish_waiting_input(tmp_path):
    from yoke.http.models.client_session import ClientSessionRequest
    from yoke.http.runtime.phase import Activity, Idle, TurnActive
    from yoke.http.services.client_session import ClientSession
    from yoke.http.services.human_input_service import HumanInputService

    registry, runtime = _runtime(tmp_path)
    human = HumanInputService(runtime.store, runtime.events)
    client = ClientSession(
        "owned",
        ClientSessionRequest(owner="test", mode="approval-required"),
        human,
        runtime.events,
        tmp_path,
    )

    async def scenario():
        execution = _execution()
        runtime.activity.enter(TurnActive(execution, Activity.TOOL), cause="test")
        loop = asyncio.get_running_loop()
        for kind in ("permission", "question"):

            def ask():
                with client.waiting(
                    lambda waiting: loop.call_soon_threadsafe(
                        runtime.activity.set_activity,
                        execution,
                        Activity.WAITING_INPUT if waiting else Activity.TOOL,
                    )
                ):
                    if kind == "question":
                        return client.question(
                            "Continue?", ["yes"], False, lambda: False
                        )
                    # Exercise the same permission policy hook as HTTP turns.
                    from yoke.agent.loop.types import BeforeToolCallContext
                    from yoke.agent.models import AgentContext, ToolCall, ToolFunction

                    return client.before_tool(None, lambda: False)(
                        BeforeToolCallContext(
                            iteration=1,
                            context=AgentContext(),
                            tool_call=ToolCall(
                                id="call",
                                function=ToolFunction(
                                    name="command_exec", arguments="{}"
                                ),
                            ),
                            arguments={},
                        )
                    )

            worker = asyncio.create_task(asyncio.to_thread(ask))
            try:
                for _ in range(100):
                    if (await runtime.status()).state == "waiting_input":
                        break
                    await asyncio.sleep(0.001)
                assert (await runtime.status()).state == "waiting_input"
            finally:
                if kind == "question":
                    for info in human.questions("owned"):
                        human.reject_question("owned", info.id)
                else:
                    for info in human.permissions("owned"):
                        human.reply_permission("owned", info.id, reply="deny")
                await worker
            assert (await runtime.status()).state == "running"
        runtime.activity.enter(Idle(), cause="test_done")

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_question_tool_worker_reports_waiting_input(tmp_path):
    from time import monotonic, sleep

    from fastapi.testclient import TestClient

    from tests.yoke.http.test_client_session import ToolProvider, app_for, configure
    from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session

    provider = ToolProvider(
        "request_user_input", {"question": "Continue?", "options": ["yes"]}
    )
    app = app_for(tmp_path, provider)
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        assert configure(client).status_code == 200
        assert (
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "ask"}, "resume": True},
            ).status_code
            == 200
        )
        human = app.state.human_input_service
        try:
            deadline = monotonic() + 1
            status = {}
            while monotonic() < deadline:
                status = (
                    client.get("/api/v1/session/active", headers=_auth())
                    .json()["data"]
                    .get("session-a", {})
                )
                if status.get("state") == "waiting_input":
                    break
                sleep(0.005)
            assert status["state"] == "waiting_input"
        finally:
            for info in human.questions("session-a"):
                human.reject_question("session-a", info.id)
            waited = client.post(
                "/api/v1/session/session-a/wait",
                headers=_auth(),
                params={"timeoutMs": 1000},
            )
            assert waited.json()["data"]["state"] == "idle"


def test_waiting_callback_preserves_client_tool_projection(tmp_path):
    from tests.yoke.http.test_attachment_continuation import RecordingProvider
    from yoke.agent.loop.tool_core import index_tools, tool_definitions
    from yoke.agent.tools.context import (
        ModelIdentity,
        ToolRegistrationContext,
        ToolSessionContext,
        normalize_tool_registration,
    )
    from yoke.http.models.client_session import ClientSessionRequest
    from yoke.http.runtime.client_tools import client_tool_factory
    from yoke.http.services.client_session import ClientSession
    from yoke.http.services.human_input_service import HumanInputService

    registry, runtime = _runtime(tmp_path)
    client = ClientSession(
        "owned",
        ClientSessionRequest(
            owner="test", mode="plan", additional_directories=[str(tmp_path)]
        ),
        HumanInputService(runtime.store, runtime.events),
        runtime.events,
        tmp_path,
    )
    register = client_tool_factory(None, ())
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=RecordingProvider(),
        model=ModelIdentity(provider_name="demo"),
        session=ToolSessionContext(client_session=client),
    )
    try:
        before = normalize_tool_registration(register(context))
        with client.waiting(lambda waiting: None):
            after = normalize_tool_registration(register(context))
        assert tool_definitions(index_tools(list(before.tools))) == tool_definitions(
            index_tools(list(after.tools))
        )
        assert before.system_messages == after.system_messages
    finally:
        registry.shutdown_executor()
