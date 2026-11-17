"""Bounded deterministic model histories against the real input SQLite store."""

from uuid import uuid4

from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
    initialize,
)

from yoke.http.models.prompt import PromptAdmissionRequest, QueuePatchRequest
from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.events import SessionEventJournal
from yoke.session.input_phase import InputEvent, Paused, PauseCause, Queued
from yoke.session.queue import prompt_queue_transaction, has_queued_prompts


def service(directory):
    store = SessionStore(directory, migrate=True)
    if store.summary_record("inputs") is None:
        store.save("inputs", [], root=directory.parent)
    events = EventService(SessionEventJournal(directory), GlobalEventBroker())
    return PendingInputService(
        store, AdmissionStore(directory), events, UploadService(store)
    )


def test_generated_input_histories(tmp_path):
    class Inputs(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.directory = tmp_path / uuid4().hex / "sessions"
            self.directory.parent.mkdir()
            self.pending = service(self.directory)
            self.phases = {}
            self.queue = []
            self.active = None
            self.counter = 0
            self.last_revision = 0

        def patch(self, operation):
            self.pending.patch_queue(
                "inputs",
                QueuePatchRequest.model_validate(
                    {
                        "expectedRevision": self.pending.queue("inputs").revision,
                        "operations": [operation],
                        "resume": False,
                    }
                ),
            )

        @rule()
        def admit(self):
            self.counter += 1
            input_id = f"input-{self.counter}"
            self.pending.admit(
                "inputs",
                PromptAdmissionRequest.model_validate(
                    {
                        "id": input_id,
                        "prompt": {"text": input_id},
                        "delivery": "queue",
                        "resume": False,
                    }
                ),
            )
            self.queue.append(input_id)
            self.phases[input_id] = "queued"

        @rule(
            action=st.sampled_from(["pause", "resume", "edit", "move", "remove"]),
            index=st.integers(0, 20),
        )
        def edit_queue(self, action, index):
            if not self.queue:
                return
            input_id = self.queue[index % len(self.queue)]
            if action in {"pause", "resume"}:
                self.patch(
                    {"op": "setPaused", "id": input_id, "paused": action == "pause"}
                )
                self.phases[input_id] = "paused" if action == "pause" else "queued"
            elif action == "edit":
                self.patch(
                    {"op": "update", "id": input_id, "prompt": {"text": "edited"}}
                )
            elif action == "move":
                self.patch({"op": "moveToStart", "id": input_id})
                self.queue.remove(input_id)
                self.queue.insert(0, input_id)
            else:
                self.patch({"op": "remove", "id": input_id})
                self.queue.remove(input_id)
                self.phases[input_id] = "removed"

        @rule()
        def promote(self):
            if self.active is not None:
                return
            self.pending.set_hold("inputs", None)
            eligible = next(
                (item for item in self.queue if self.phases[item] == "queued"), None
            )
            admission = self.pending.pop_next("inputs", allow_queue=True)
            assert (admission.id if admission else None) == eligible
            if eligible:
                self.active = eligible
                self.queue.remove(eligible)
                self.phases[eligible] = "promoted"

        @rule()
        def start(self):
            if self.active and self.phases[self.active] == "promoted":
                self.pending.start_input("inputs", self.active, 7)
                self.phases[self.active] = "running"

        @rule()
        def block(self):
            if self.active:
                record = self.pending.get_input("inputs", self.active)
                position = record.queue_position
                assert position is not None
                with prompt_queue_transaction(self.directory, "inputs") as tx:
                    self.pending.lifecycle.apply(
                        tx,
                        "inputs",
                        record,
                        InputEvent.BLOCK_WORKSPACE,
                        Paused(position, PauseCause.WORKSPACE),
                    )
                    self.pending.lifecycle.reindex(tx, "inputs")
                self.queue.insert(min(position, len(self.queue)), self.active)
                self.phases[self.active] = "paused"
                self.active = None

        @rule(outcome=st.sampled_from(["completed", "stopped", "failed", "recovered"]))
        def settle(self, outcome):
            if self.active:
                self.pending.settle("inputs", self.active, outcome=outcome)
                self.phases[self.active] = "settled"
                self.active = None

        @rule()
        def recover(self):
            if self.active:
                self.pending.recover("inputs", self.active)
                self.phases[self.active] = "promoted"

        @rule(held=st.booleans())
        def hold(self, held):
            self.pending.set_hold("inputs", "interrupt" if held else None)

        @rule()
        def crash(self):
            self.pending.store.database.close()
            self.pending = service(self.directory)
            self.pending.hold_stranded()

        @initialize()
        def important_transitions(self):
            # Guaranteed coverage does not depend on Hypothesis's choice of rules.
            self.admit()
            self.edit_queue("pause", 0)
            self.edit_queue("resume", 0)
            self.admit()
            self.edit_queue("move", 1)
            self.edit_queue("edit", 0)
            self.promote()
            self.start()
            self.block()
            self.edit_queue("resume", 0)
            self.promote()
            self.crash()
            self.recover()
            self.settle("recovered")
            self.edit_queue("remove", 0)
            self.consistent_projection()

        @invariant()
        def consistent_projection(self):
            pending = self.pending
            snapshot = pending.admissions.load("inputs")
            assert {
                key: record.phase.kind for key, record in snapshot.records.items()
            } == self.phases
            queue = pending.queue("inputs")
            assert [item.id for item in queue.items] == self.queue
            assert [item.paused for item in queue.items] == [
                self.phases[key] == "paused" for key in self.queue
            ]
            for position, key in enumerate(self.queue):
                phase = snapshot.records[key].phase
                assert isinstance(phase, (Queued, Paused))
                assert phase.position == position
            assert (
                sum(phase in {"promoted", "running"} for phase in self.phases.values())
                <= 1
            )
            for record in snapshot.records.values():
                assert record.settled == (record.outcome is not None)
                assert record.settled == (record.phase.kind == "settled")
            events, more = pending.events.journal.history("inputs", limit=10000)
            assert not more
            changes = [
                event.data for event in events if event.type == "session.input.changed"
            ]
            assert [event["inputRevision"] for event in changes] == list(
                range(1, queue.input_revision + 1)
            )
            assert queue.input_revision >= self.last_revision
            self.last_revision = queue.input_revision
            assert has_queued_prompts(self.directory) == (
                queue.held is None
                and any(self.phases[key] == "queued" for key in self.queue)
            )
            with pending.store.database.read() as connection:
                rows = connection.execute(
                    "SELECT id,phase,position FROM inputs WHERE session_id='inputs'"
                ).fetchall()
            assert {key: phase for key, phase, _ in rows} == self.phases
            assert {key for key, phase, pos in rows if pos is not None} == set(
                self.queue
            )

        def teardown(self):
            self.pending.store.database.close()

    run_state_machine_as_test(
        Inputs,
        settings=settings(
            derandomize=True,
            max_examples=4,
            stateful_step_count=8,
            deadline=500,
        ),
    )
