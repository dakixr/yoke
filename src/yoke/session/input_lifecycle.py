"""Atomic input phase ownership, queue projection and transition publication."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from yoke.session.admissions import AdmissionRecord, AdmissionStore
from yoke.session.events import SessionEventJournal
from yoke.session.input_phase import (
    IllegalTransition,
    InputEvent,
    InputPhase,
    Paused,
    Promoted,
    Queued,
    Running,
    Settled,
    next_phase,
)
from yoke.session.queue import PersistedPendingInput, PromptQueueTransaction

LOGGER = logging.getLogger(__name__)


class InputEvents(Protocol):
    """Durable journal port. Publication must wait for the caller's commit."""

    def durable(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, object],
        *,
        location: str | None = None,
    ) -> object: ...


class JournalEvents:
    """Journal-only port for offline workspace repair."""

    def __init__(self, admissions: AdmissionStore) -> None:
        self.journal = SessionEventJournal(admissions.session_directory)

    def durable(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, object],
        *,
        location: str | None = None,
    ) -> object:
        return self.journal.append(session_id, event_type, data, location=location)


class InputLifecycle:
    """Own phase writes inside the queue transaction supplied by the caller."""

    def __init__(
        self, admissions: AdmissionStore, events: InputEvents | None = None
    ) -> None:
        self.admissions = admissions
        self.events = events or JournalEvents(admissions)

    def apply(
        self,
        tx: PromptQueueTransaction,
        session_id: str,
        record: AdmissionRecord,
        event: InputEvent,
        target: InputPhase,
        *,
        replacing: bool = False,
    ) -> InputPhase:
        """Validate and commit one phase and its legacy queue projection together."""
        snapshot = self.admissions.load(session_id)
        prior = snapshot.records.get(record.id)
        previous = prior.phase if prior else None
        target = next_phase(previous, event, target)
        if isinstance(target, (Promoted, Running)) and not replacing:
            if any(
                item.id != record.id and isinstance(item.phase, (Promoted, Running))
                for item in snapshot.records.values()
            ):
                raise IllegalTransition("Another input already owns this session")
        record.phase = target
        if isinstance(target, (Promoted, Running)):
            record.promoted_seq = target.promoted_seq
        queue = tx.snapshot
        queue.prompts = [item for item in queue.prompts if item.id != record.id]
        if isinstance(target, (Queued, Paused)):
            queue.prompts.insert(
                min(target.position, len(queue.prompts)),
                PersistedPendingInput(
                    id=record.id,
                    prompt=record.prompt,
                    continuation=record.continuation,
                    skill=record.skill,
                    attachments=[item.model_dump() for item in record.attachments],
                    kind="steering" if record.delivery == "steer" else "queued",
                    created_at=record.time_created,
                    paused=isinstance(target, Paused),
                    pause_cause=target.cause if isinstance(target, Paused) else None,
                ),
            )
        snapshot.records[record.id] = record
        self.admissions.save(session_id, snapshot)
        queue.input_revision += 1
        tx.commit()
        self.events.durable(
            session_id,
            "session.input.changed",
            {
                "inputID": record.id,
                "phase": target.kind,
                "previous": previous.kind if previous else None,
                "cause": event.value,
                "turnID": target.turn_id if isinstance(target, Running) else None,
                "position": target.position
                if isinstance(target, (Queued, Paused))
                else None,
                "delivery": record.delivery,
                "outcome": target.outcome if isinstance(target, Settled) else None,
                "pauseCause": target.cause.value
                if isinstance(target, Paused)
                else None,
                "inputRevision": queue.input_revision,
                "clientID": record.client_id,
                "prompt": record.prompt,
            },
        )
        LOGGER.debug(
            "machine=input id=%s from=%s to=%s cause=%s revision=%s",
            record.id,
            previous.kind if previous else None,
            target.kind,
            event.value,
            queue.input_revision,
        )
        return target

    def reindex(self, tx: PromptQueueTransaction, session_id: str) -> None:
        """Record position changes of retained inputs after queue mutations."""
        snapshot = self.admissions.load(session_id)
        for position, item in enumerate(list(tx.snapshot.prompts)):
            record = snapshot.records.get(item.id)
            if (
                record
                and isinstance(record.phase, (Queued, Paused))
                and record.phase.position != position
            ):
                self.apply(
                    tx,
                    session_id,
                    record,
                    InputEvent.MOVE,
                    replace(record.phase, position=position),
                )
