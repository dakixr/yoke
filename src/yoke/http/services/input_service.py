"""Input snapshots, durable queue holds and execution boundaries."""

from __future__ import annotations

from yoke.http.errors import ApiError
from yoke.http.models.prompt import (
    SessionInputs,
    InputInfo,
    PromptAdmissionReceipt,
    QueueHold,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.prompt_queue import receipt
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.input_lifecycle import InputLifecycle
from yoke.session.input_phase import InputEvent, Paused, Promoted, Queued, Running
from yoke.session.queue import prompt_queue_transaction


class InputService:
    """Read one consistent input snapshot and own holds independently of runtimes."""

    def __init__(
        self,
        store: SessionStore,
        admissions: AdmissionStore,
        events: EventService,
        lifecycle: InputLifecycle,
    ) -> None:
        self.store = store
        self.admissions = admissions
        self.events = events
        self.lifecycle = lifecycle

    def get_input(self, session_id: str, input_id: str):  # noqa: ANN201
        if self.store.summary_record(session_id) is None:
            from yoke.http.errors import session_not_found

            raise session_not_found()
        record = self.admissions.load(session_id).records.get(input_id)
        if record is None:
            raise ApiError(404, "input_not_found", "Input was not found.")
        return record

    def receipt(self, session_id: str, input_id: str) -> PromptAdmissionReceipt:
        return receipt(self.get_input(session_id, input_id))

    def snapshot(self, session_id: str, *, live: bool = True) -> SessionInputs:
        if self.store.summary_record(session_id) is None:
            from yoke.http.errors import session_not_found

            raise session_not_found()
        with prompt_queue_transaction(self.store.directory, session_id) as tx:
            records = self.admissions.load(session_id).records.values()
            return SessionInputs(
                input_revision=tx.snapshot.input_revision,
                held=QueueHold(reason=tx.snapshot.held_reason)
                if tx.snapshot.held_reason
                else None,
                inputs=[
                    self.info(record)
                    for record in records
                    if not live or record.phase.kind not in {"settled", "removed"}
                ],
            )

    @staticmethod
    def info(record) -> InputInfo:  # noqa: ANN001
        phase = record.phase
        return InputInfo(
            id=record.id,
            phase=phase.kind,
            delivery=record.delivery,
            position=phase.position if isinstance(phase, (Queued, Paused)) else None,
            pause_cause=phase.cause if isinstance(phase, Paused) else None,
            turn_id=phase.turn_id if isinstance(phase, Running) else None,
            outcome=record.outcome,
            client_id=record.client_id,
            prompt=record.prompt,
        )

    def set_hold(self, session_id: str, reason: str | None) -> None:
        with prompt_queue_transaction(self.store.directory, session_id) as tx:
            if tx.snapshot.held_reason == reason:
                return
            tx.snapshot.held_reason = reason
            tx.commit()
            self.events.durable(
                session_id,
                "session.queue.held",
                {
                    "inputRevision": tx.snapshot.input_revision,
                    "held": {"reason": reason} if reason else None,
                },
            )

    def hold_stranded(self) -> None:
        with self.admissions.database.read() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT i.session_id FROM inputs i JOIN sessions s ON s.id=i.session_id "
                    "WHERE i.position IS NOT NULL AND i.paused=0"
                )
            ]
        for session_id in ids:
            self.set_hold(session_id, "daemon_restart")

    def start(self, session_id: str, input_id: str, turn_id: int) -> None:
        with prompt_queue_transaction(self.store.directory, session_id) as tx:
            record = self.get_input(session_id, input_id)
            phase = record.phase
            if not isinstance(phase, Promoted):
                return
            tx.snapshot.held_reason = None
            self.lifecycle.apply(
                tx,
                session_id,
                record,
                InputEvent.START,
                Running(phase.promoted_seq, phase.queue_position, turn_id),
                replacing=True,
            )

    def recover(self, session_id: str, input_id: str):  # noqa: ANN201
        with prompt_queue_transaction(self.store.directory, session_id) as tx:
            record = self.get_input(session_id, input_id)
            phase = record.phase
            if isinstance(phase, (Promoted, Running)):
                self.lifecycle.apply(
                    tx,
                    session_id,
                    record,
                    InputEvent.RECOVER_RESUME,
                    Promoted(phase.promoted_seq, phase.queue_position),
                    replacing=True,
                )
            return record
