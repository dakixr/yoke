"""Durable prompt-admission identity records."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import Field, model_serializer, model_validator

from yoke.session.input_phase import (
    InputPhase,
    Queued,
    Paused,
    Promoted,
    Running,
    Settled,
    Removed,
    PauseCause,
)

from yoke.session.inputs import ensure_input_row, session_database, sync_input_row

INPUT_ID_METADATA_KEY = "yoke_input_id"


class AdmissionAttachment(BaseModel):
    """Durable uploaded attachment reference attached to one admitted input."""

    uri: str
    name: str
    mime: str
    image_projection: Literal["cli"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class AdmissionRecord(BaseModel):
    """Durable identity and lifecycle for one submitted input."""

    id: str
    session_id: str
    prompt: str
    attachments: list[AdmissionAttachment] = Field(default_factory=list)
    continuation: bool = False
    skill: str | None = Field(default=None, exclude_if=lambda value: value is None)
    delivery: Literal["steer", "queue"]
    fingerprint: str
    time_created: str
    phase: InputPhase = Field(default_factory=lambda: Queued(0))
    admitted_seq: int
    promoted_seq: int | None = None
    client_id: str | None = None

    def __init__(self, **data: Any) -> None:  # noqa: ANN401
        # Legacy callers can still supply state and settlement projection fields.
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def legacy_phase(cls, value):  # noqa: ANN001, ANN206
        """Read pre-v6 records and legacy in-process constructors once."""
        if isinstance(value, dict) and "phase" not in value:
            value = dict(value)
            position = value.get("queue_position") or 0
            if value.get("state") == "removed":
                phase = Removed()
            elif value.get("settled"):
                phase = Settled(
                    value.get("outcome") or "recovered", value.get("settled_at") or ""
                )
            elif value.get("workspace_blocked"):
                phase = Paused(position, PauseCause.WORKSPACE)
            elif value.get("state") == "promoted":
                phase = Promoted(value.get("promoted_seq") or 0, position)
            else:
                phase = Queued(position)
            value["phase"] = phase
        return value

    @property
    def state(self) -> Literal["admitted", "promoted", "removed"]:
        if isinstance(self.phase, Removed):
            return "removed"
        return "admitted" if isinstance(self.phase, (Queued, Paused)) else "promoted"

    @property
    def settled(self) -> bool:
        return isinstance(self.phase, Settled)

    @property
    def outcome(self):  # noqa: ANN201
        return self.phase.outcome if isinstance(self.phase, Settled) else None

    @property
    def settled_at(self) -> str | None:
        return self.phase.settled_at if isinstance(self.phase, Settled) else None

    @property
    def workspace_blocked(self) -> bool:
        return (
            isinstance(self.phase, Paused) and self.phase.cause == PauseCause.WORKSPACE
        )

    @property
    def queue_position(self) -> int | None:
        if isinstance(self.phase, (Promoted, Running)):
            return self.phase.queue_position
        if isinstance(self.phase, (Queued, Paused)):
            return self.phase.position
        return None

    @model_serializer(mode="wrap")
    def legacy_projection(self, handler):  # noqa: ANN001, ANN201
        result = handler(self)
        result.update(
            state=self.state,
            settled=self.settled,
            outcome=self.outcome,
            settled_at=self.settled_at,
            workspace_blocked=self.workspace_blocked,
            queue_position=self.queue_position,
        )
        return result


class AdmissionSnapshot(BaseModel):
    """Per-session admission identity table."""

    version: int = 1
    records: dict[str, AdmissionRecord] = Field(default_factory=dict)


class AdmissionStore:
    """Per-session admission records in the shared ``inputs`` table."""

    def __init__(self, session_directory: Path) -> None:
        self.session_directory = session_directory.resolve()
        self.database = session_database(self.session_directory)

    def load(self, session_id: str) -> AdmissionSnapshot:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT admission FROM inputs "
                "WHERE session_id = ? AND admission IS NOT NULL "
                "ORDER BY admitted_seq, id",
                (session_id,),
            ).fetchall()
        records = [AdmissionRecord.model_validate_json(row[0]) for row in rows]
        return AdmissionSnapshot(records={record.id: record for record in records})

    def save(self, session_id: str, snapshot: AdmissionSnapshot) -> None:
        with self.database.write() as connection:
            stored = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM inputs "
                    "WHERE session_id = ? AND admission IS NOT NULL",
                    (session_id,),
                )
            ]
            for input_id in set(stored) - set(snapshot.records):
                connection.execute(
                    "UPDATE inputs SET admission = NULL "
                    "WHERE session_id = ? AND id = ?",
                    (session_id, input_id),
                )
            for input_id, record in snapshot.records.items():
                ensure_input_row(connection, session_id, input_id)
                connection.execute(
                    "UPDATE inputs SET admission = ? WHERE session_id = ? AND id = ?",
                    (record.model_dump_json(), session_id, input_id),
                )
            for input_id in dict.fromkeys([*stored, *snapshot.records]):
                sync_input_row(connection, session_id, input_id)
