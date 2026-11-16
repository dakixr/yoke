"""Durable prompt-admission identity records."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

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
    state: Literal["admitted", "promoted", "removed"] = "admitted"
    admitted_seq: int
    promoted_seq: int | None = None
    settled: bool = False
    settled_at: str | None = None
    outcome: Literal["completed", "stopped", "failed", "recovered"] | None = None
    workspace_blocked: bool = False
    queue_position: int | None = None


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
