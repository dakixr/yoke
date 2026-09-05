"""Durable prompt-admission identity records."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from yoke._file_io import atomic_write_text


INPUT_ID_METADATA_KEY = "yoke_input_id"


class AdmissionAttachment(BaseModel):
    """Durable uploaded attachment reference attached to one admitted input."""

    uri: str
    name: str
    mime: str


class AdmissionRecord(BaseModel):
    """Durable identity and lifecycle for one submitted input."""

    id: str
    session_id: str
    prompt: str
    attachments: list[AdmissionAttachment] = Field(default_factory=list)
    delivery: Literal["steer", "queue"]
    fingerprint: str
    time_created: str
    state: Literal["admitted", "promoted", "removed"] = "admitted"
    admitted_seq: int
    promoted_seq: int | None = None
    settled: bool = False
    settled_at: str | None = None
    outcome: Literal["completed", "stopped", "failed", "recovered"] | None = None


class AdmissionSnapshot(BaseModel):
    """Per-session admission identity table."""

    version: int = 1
    records: dict[str, AdmissionRecord] = Field(default_factory=dict)


class AdmissionStore:
    """Atomic per-session admission snapshot repository."""

    def __init__(self, session_directory: Path) -> None:
        self.session_directory = session_directory.resolve()
        self._locks_lock = Lock()
        self._locks: dict[str, Lock] = {}

    def load(self, session_id: str) -> AdmissionSnapshot:
        path = self._path(session_id)
        with self._lock_for(session_id):
            if not path.exists():
                return AdmissionSnapshot()
            try:
                return AdmissionSnapshot.model_validate_json(path.read_text("utf-8"))
            except (OSError, ValueError):
                return AdmissionSnapshot()

    def save(self, session_id: str, snapshot: AdmissionSnapshot) -> None:
        path = self._path(session_id)
        with self._lock_for(session_id):
            atomic_write_text(path, snapshot.model_dump_json(indent=2))

    def _path(self, session_id: str) -> Path:
        return self.session_directory / "inputs" / f"{session_id}.json"

    def _lock_for(self, session_id: str) -> Lock:
        with self._locks_lock:
            return self._locks.setdefault(session_id, Lock())
