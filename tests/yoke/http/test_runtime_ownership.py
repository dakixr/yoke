"""Loop confinement, blocking storage, and explicit session context regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event, Thread, get_ident
from time import monotonic

import pytest

from tests.yoke.http.test_attachment_continuation import RecordingProvider
from yoke.agent.context import ContextManager
from yoke.agent.loop import AgentResult, RuntimeAgent
from yoke.agent.loop.tool_core import tool_definitions
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.session_tree import SessionTree
from yoke.agent.tools.context import (
    ToolRegistrationContext,
    ToolRegistrationResult,
    ToolSessionContext,
)
from yoke.agent.tools.read import ReadTool
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.services.session_runtime.execution import TurnExecution, TurnOutcome
from yoke.mcp.config import McpSessionPolicy, McpSessionServerPolicy
from yoke.session.admissions import AdmissionRecord


def _runtime(tmp_path: Path):  # noqa: ANN202
    app = create_app(
        HttpAppSettings(
            auth_token=None,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(RecordingProvider(), []),
        )
    )
    registry = app.state.runtime_registry
    registry.store.save("owned", [], root=tmp_path, title="Named")
    registry.pending_inputs.admit(
        "owned",
        PromptAdmissionRequest.model_validate(
            {"id": "input", "prompt": {"text": "hello"}, "resume": False}
        ),
    )
    assert registry.pending_inputs.pop_next("owned", allow_queue=True) is not None
    return registry, registry.get_or_start("owned")


def _execution() -> TurnExecution:
    return TurnExecution(
        turn_id=1,
        admission=AdmissionRecord(
            id="input",
            session_id="owned",
            prompt="hello",
            delivery="queue",
            fingerprint="fixture",
            time_created="2026-01-01T00:00:00+00:00",
            admitted_seq=1,
        ),
        started_at="2026-01-01T00:00:00+00:00",
        started_monotonic=monotonic(),
        stop_event=Event(),
        retired_event=Event(),
        cold_start=False,
        automatic_title=False,
    )


def test_queued_worker_event_flood_cannot_publish_after_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, runtime = _runtime(tmp_path)
    published: list[tuple[str, int]] = []
    monkeypatch.setattr(
        runtime.events,
        "live",
        lambda event, *_args, **_kwargs: published.append((event, get_ident())),
    )

    async def scenario() -> None:
        execution = _execution()
        runtime.activity.active = execution
        runtime.activity.state = "running"
        loop = asyncio.get_running_loop()
        loop_thread = get_ident()

        def flood() -> None:
            for index in range(500):
                runtime.agent_events.receive(
                    loop,
                    execution,
                    "tool_execution_start",
                    {"tool_call_id": f"call-{index}", "tool_name": "read"},
                )
            runtime.agent_events.receive(
                loop, execution, "assistant_message", {"content": "late"}
            )

        thread = Thread(target=flood)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        # The loop has not yielded: receiving events must never publish or mutate
        # loop-owned tool counts/activity on the worker.
        assert published == []
        assert execution.tool_count == 0
        assert await runtime.interrupt() == (True, 1)
        await asyncio.sleep(0)
        assert execution.retired_event.is_set()
        assert execution.tool_count == 0
        assert all(thread_id == loop_thread for _, thread_id in published)
        assert [event for event, _ in published] == ["session.active.changed"]
        await registry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["finish", "interrupt", "mutation", "title"])
def test_storage_waits_do_not_block_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    registry, runtime = _runtime(tmp_path)
    started, release = Event(), Event()

    async def scenario() -> None:
        loop_thread = get_ident()

        def blocked(*_args, **_kwargs) -> None:
            assert get_ident() != loop_thread, "storage ran on the event loop"
            started.set()
            assert release.wait(3), "test failed to release storage"

        execution = _execution()
        runtime.activity.active = (
            execution if stage in {"finish", "interrupt"} else None
        )
        if stage == "finish":
            monkeypatch.setattr(runtime.persistence, "persist_turn_entries", blocked)
            outcome = TurnOutcome(
                agent=None,
                result=AgentResult(
                    output="done",
                    messages=[],
                    iterations=1,
                    conversation_entries=[
                        ConversationEntry(
                            kind="assistant", message=Message.assistant("done")
                        )
                    ],
                ),
            )
            operation = runtime.finalizer.finish(execution, outcome)
        elif stage == "interrupt":
            monkeypatch.setattr(runtime.persistence, "persist_interruption", blocked)
            operation = runtime.interrupt()
        elif stage == "title":
            registry.store.set_title("owned", None)
            monkeypatch.setattr(
                runtime.automatic_title,
                "_generate_sync",
                lambda _admission: "Generated",
            )
            original = registry.store.set_title

            def save_title(*args, **kwargs):  # noqa: ANN202
                blocked()
                return original(*args, **kwargs)

            monkeypatch.setattr(registry.store, "set_title", save_title)
            operation = runtime.automatic_title._generate(execution.admission)
        else:
            operation = runtime.idle_mutation(blocked)
        task = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not task.done()
            # A separate coroutine can run while the asyncio gate excludes starts.
            assert (
                await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.2)
                == "responsive"
            )
        finally:
            release.set()
            await task
            await registry.close()

    asyncio.run(scenario())


def test_cancelled_idle_mutation_keeps_gate_until_storage_finishes(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    started, release = Event(), Event()

    def mutate() -> None:
        started.set()
        assert release.wait(3)

    async def scenario() -> None:
        task = asyncio.create_task(runtime.idle_mutation(mutate))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert runtime.gate.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime.gate.locked()
        await registry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("isolate", [False, True])
def test_session_context_survives_fork_without_provider_payload_changes(
    tmp_path: Path, isolate: bool
) -> None:
    class ForkingProvider(RecordingProvider):
        def fork_for_turn(self) -> ForkingProvider:
            forked = ForkingProvider()
            forked.calls = self.calls
            return forked

    provider = ForkingProvider()
    contexts: list[ToolRegistrationContext] = []
    policy = McpSessionPolicy.empty()
    policy.servers["fixture"] = McpSessionServerPolicy(enabled=False)
    session = ToolSessionContext(mcp_policy=policy)

    def register(context: ToolRegistrationContext) -> ToolRegistrationResult:
        contexts.append(context)
        return ToolRegistrationResult(
            tools=[ReadTool(path="example.txt")],
            system_messages=[Message.system("Existing tool instructions")],
        )

    manager = ContextManager(instructions=[Message.system("Existing system prompt")])
    agent = RuntimeAgent(
        provider,
        [],
        context_manager=manager,
        tool_factory=register,
        tool_root=tmp_path,
        tool_session_context=session,
    )
    fork = None
    try:
        definitions = json.dumps(tool_definitions(agent.tools), sort_keys=True)
        instructions = [message.model_dump_json() for message in manager.instructions]
        assert definitions == json.dumps(
            [ReadTool(path="example.txt").to_definition()], sort_keys=True
        )
        assert instructions == [
            Message.system("Existing system prompt").model_dump_json(),
            Message.system("Existing tool instructions").model_dump_json(),
        ]
        agent.load_conversation(
            messages=[Message.user("history"), Message.assistant("answer")]
        )
        fork = agent.fork(isolate_provider=isolate)
        assert (fork.provider is not provider) is isolate
        assert fork.tool_session_context is session
        assert contexts[-1].session is session
        assert fork.tools["read"].context.session is session
        assert not hasattr(fork.provider, "_yoke_mcp_session_policy")
        assert not hasattr(fork.provider, "_yoke_client_session")
        assert json.dumps(tool_definitions(fork.tools), sort_keys=True) == definitions
        assert [
            message.model_dump_json() for message in fork.context_manager.instructions
        ] == instructions
        # Compare the actual provider request, including historical instruction
        # placement. Forking must leave provider projection and ordering intact.
        agent.run("next")
        expected = [message.model_dump_json() for message in provider.calls[-1]]
        fork.run("next")
        assert [message.model_dump_json() for message in provider.calls[-1]] == expected
    finally:
        if fork is not None:
            fork.close()
        agent.close()


def test_injected_factory_uses_indexed_runtime_seed_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, runtime = _runtime(tmp_path)
    assert runtime.indexed_runtime_seed is True
    tree = SessionTree.from_messages(
        [Message.user("history"), Message.assistant("answer")]
    )
    tree.append_checkpoint("summary", retained_messages=[Message.user("history")])
    exported = tree.export_for_persistence()
    registry.store.save(
        "owned",
        [],
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=tmp_path,
        title="Named",
    )
    assert (
        runtime.message_index.page("owned", limit=1, order="asc", anchor_id=None)
        is not None
    )
    calls: list[str] = []
    original = runtime.message_index.runtime_seed

    def seed(session_id):  # noqa: ANN202
        calls.append(session_id)
        return original(session_id)

    monkeypatch.setattr(runtime.message_index, "runtime_seed", seed)

    def reject_snapshot():  # noqa: ANN202
        raise AssertionError("indexed turn unexpectedly used the legacy snapshot")

    monkeypatch.setattr(runtime.persistence, "snapshot", reject_snapshot)

    async def scenario() -> None:
        execution = _execution()
        runtime.activity.active = execution
        outcome = await runtime.run_blocking(
            runtime.turn_worker.execute, execution, asyncio.get_running_loop()
        )
        assert outcome.error is None
        assert execution.append_persistence is not None
        assert calls == ["owned"]
        runtime.resources.retire(outcome.agent)
        runtime.activity.active = None
        await registry.close()

    asyncio.run(scenario())
