"""Bounded generated runtime histories exercise real storage and fake turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from tests.yoke.http.fake_session_agent import FakeSessionAgent
from tests.yoke.http.test_runtime_ownership import _runtime
from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.runtime.phase import Idle


@dataclass
class Script:
    fail: bool = False
    started: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)


class ScriptedAgent(FakeSessionAgent):
    def __init__(self, record, script):
        super().__init__(record)
        self.script = script

    def run(self, prompt, *, user_message=None, **kwargs):
        self.script.started.set()
        assert self.script.release.wait(2)
        if self.script.fail:
            raise RuntimeError("scripted turn failure")
        tree = SessionTree.restore(
            self.record.conversation_entries, self.record.leaf_id
        )
        tree.append_message(user_message or Message.user(prompt))
        tree.append_message(Message.assistant("done"))
        return AgentResult(
            output="done",
            messages=[],
            iterations=1,
            conversation_entries=list(tree.entries),
        )


def test_generated_runtime_histories(tmp_path: Path):
    class RuntimeMachine(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.loop = asyncio.new_event_loop()
            root = tmp_path / uuid4().hex
            root.mkdir()
            self.registry, self.runtime = _runtime(root)
            self.runtime.pending_inputs.settle("owned", "input", outcome="recovered")
            self.script = Script()
            self.script.release.set()
            self.runtime.resources.agent_factory = lambda record: ScriptedAgent(
                record, self.script
            )
            self.runtime.read_cache.get("owned")
            self.events = []
            self.runtime.events.broker.publish = lambda event, **kwargs: (
                self.events.append(event)
            )
            self.counter = 0

        def run(self, coroutine):
            return self.loop.run_until_complete(coroutine)

        def admit(self, *, steer=False):
            self.counter += 1
            self.runtime.pending_inputs.admit(
                "owned",
                PromptAdmissionRequest.model_validate(
                    {
                        "id": f"input-{self.counter}",
                        "prompt": {"text": "hello"},
                        "delivery": "steer" if steer else "queue",
                        "resume": False,
                    }
                ),
            )

        @rule(fail=st.booleans())
        def turn(self, fail):
            self.script.fail = fail
            self.admit()
            self.run(self.runtime.wake())
            assert self.run(self.runtime.wait(1)).state == ("error" if fail else "idle")

        @rule(steer=st.booleans(), persist_fails=st.booleans())
        def stop_or_steer(self, steer, persist_fails):
            self.script.fail = False
            self.script.started.clear()
            self.script.release.clear()
            self.admit()

            async def scenario():
                await self.runtime.wake()
                assert await asyncio.to_thread(self.script.started.wait, 1)
                original = self.runtime.persistence.persist_interruption
                if persist_fails:

                    def fail(*args, **kwargs):
                        raise OSError("scripted stop write failure")

                    self.runtime.persistence.persist_interruption = fail
                try:
                    if steer:
                        self.admit(steer=True)
                        action = self.runtime.wake()
                    else:
                        action = self.runtime.interrupt()
                    if persist_fails:
                        with pytest.raises(OSError):
                            await action
                    else:
                        await action
                finally:
                    self.runtime.persistence.persist_interruption = original
                    self.script.release.set()
                await self.runtime.wait(1)
                assert await self.runtime.workers.drain(1) == 0

            self.run(scenario())

        @rule(outcome=st.sampled_from(["success", "failure", "journal", "cancel"]))
        def compaction(self, outcome):
            original_sync = self.runtime.operations._compact_sync
            original_durable = self.runtime.events.durable

            def compact():
                if outcome == "failure":
                    raise RuntimeError("scripted compaction failure")
                return {"compacted": True}

            def durable(session_id, event_type, *args, **kwargs):
                if outcome == "journal" and event_type == "session.compaction.started":
                    raise OSError("scripted journal failure")
                return original_durable(session_id, event_type, *args, **kwargs)

            self.runtime.operations._compact_sync = compact
            self.runtime.events.durable = durable

            async def scenario():
                if outcome == "journal":
                    with pytest.raises(OSError):
                        await self.runtime.compact()
                else:
                    await self.runtime.compact()
                    if outcome == "cancel":
                        task = self.runtime.activity.operation.task
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await self.runtime.wait(1)

            try:
                self.run(scenario())
            finally:
                self.runtime.operations._compact_sync = original_sync
                self.runtime.events.durable = original_durable

        @invariant()
        def quiescent_lane_and_ordered_publication(self):
            activity = self.runtime.activity
            activity.check_invariants()
            assert self.registry.active_slots._value == 50
            assert isinstance(activity.phase, Idle)
            assert activity.active is None and activity.operation is None
            changes = [
                event.data
                for event in self.events
                if event.type == "session.active.changed"
            ]
            assert [event["revision"] for event in changes] == list(
                range(1, activity.revision + 1)
            )
            assert not changes or changes[-1]["state"] in {"idle", "error"}
            # Stop, failed persistence and cancellation intentionally leave work
            # stopped; phase 2 makes this existing hold durable.
            if self.runtime.pending_inputs.queue("owned").items:
                assert activity.phase.hold is not None

        def teardown(self):
            self.script.release.set()
            self.run(self.registry.close())
            self.loop.close()

    run_state_machine_as_test(
        RuntimeMachine,
        settings=settings(
            derandomize=True,
            max_examples=12,
            stateful_step_count=12,
            deadline=None,
        ),
    )
