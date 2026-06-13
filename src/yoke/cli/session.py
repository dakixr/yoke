"""CLI-owned JSONL session persistence."""

from __future__ import annotations

import builtins
import json
import os
import re
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from yoke.agent.state import migrate_conversation_tree
from yoke.agent.state import transcript_messages_from_entries
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.session_tree import _raw_record_missing_tree_fields
from yoke.cli.session_tree import _resolve_saved_conversation_tree
from yoke.cli.session_tree import _sanitize_conversation_entries
from yoke.agent.skills.models import ActiveSkill

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_INDEX_NAME = "index.json"
SESSION_FILE_SUFFIX = ".jsonl"
LEGACY_SESSION_FILE_SUFFIX = ".json"
SESSION_RETENTION_DAYS = 30


class SessionRecord(BaseModel):
    """Persisted CLI session state."""

    version: int = 4
    id: str
    conversation_entries: list[ConversationEntry] = Field(default_factory=list)
    leaf_id: str | None = None
    active_skills: list[ActiveSkill] = Field(default_factory=list)
    skill_dirs: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    root: str | None = None
    title: str | None = None
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None

    @property
    def messages(self) -> list[Message]:
        """Transcript messages in the session."""
        return transcript_messages_from_entries(
            self.conversation_entries,
            leaf_id=self.leaf_id,
        )


class SessionIndexEntry(BaseModel):
    """Searchable session summary stored in the CLI session index."""

    id: str
    root: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_record(self) -> SessionRecord:
        """Convert the index entry into a partial session record."""
        return SessionRecord(
            id=self.id,
            root=self.root,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class SessionIndex(BaseModel):
    """Persistent index of saved CLI sessions."""

    sessions: dict[str, SessionIndexEntry] = Field(default_factory=dict)


class SessionStore:
    """JSONL-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()
        self._migrate_legacy_sessions()

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record."""
        path = self._existing_session_path(session_id)
        if path is None:
            path = self._session_path(session_id)
        if not path.exists():
            return SessionRecord(id=session_id)
        try:
            raw_text = path.read_text(encoding="utf-8")
            record = self._decode_session_record(raw_text)
        except (OSError, ValidationError) as exc:
            raise ValueError(f"Failed to load session {session_id!r}: {exc}") from exc
        raw_missing_tree_fields = _raw_record_missing_tree_fields(raw_text)
        sanitized_entries = _sanitize_conversation_entries(record.conversation_entries)
        migrated_entries, leaf_id, tree_changed = migrate_conversation_tree(
            sanitized_entries,
            leaf_id=record.leaf_id,
            assume_linear=raw_missing_tree_fields,
        )
        if (
            raw_missing_tree_fields
            or sanitized_entries != record.conversation_entries
            or tree_changed
            or record.version < 4
            or path.suffix != SESSION_FILE_SUFFIX
        ):
            record = record.model_copy(
                update={
                    "version": 4,
                    "conversation_entries": migrated_entries,
                    "leaf_id": leaf_id,
                }
            )
            path = self._write_session_record(record)
            self._update_index(record)
        return record

    def save(
        self,
        session_id: str,
        messages: builtins.list[Message],
        *,
        conversation_entries: builtins.list[ConversationEntry] | None = None,
        leaf_id: str | None = None,
        active_skills: builtins.list[ActiveSkill] | None = None,
        skill_dirs: builtins.list[str] | None = None,
        root: Path | str | None = None,
        title: str | None = None,
        provider_name: str | None = None,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        context_window_tokens: int | None = None,
    ) -> Path:
        """Save a session record."""
        self._prune_index_and_sessions(exclude_session_id=session_id)
        path = self._session_path(session_id)
        existing_path = self._existing_session_path(session_id)
        existing = (
            self.load(session_id)
            if existing_path is not None
            else SessionRecord(id=session_id)
        )
        now = _timestamp()
        resolved_entries, resolved_leaf_id = _resolve_saved_conversation_tree(
            existing,
            messages,
            conversation_entries=conversation_entries,
            leaf_id=leaf_id,
        )
        record = SessionRecord(
            id=session_id,
            conversation_entries=resolved_entries,
            leaf_id=resolved_leaf_id,
            active_skills=list(active_skills or existing.active_skills),
            skill_dirs=list(skill_dirs or existing.skill_dirs),
            created_at=existing.created_at or now,
            updated_at=now,
            root=_normalize_root(root) or existing.root,
            title=_normalize_title(title) or existing.title,
            provider_name=provider_name or existing.provider_name,
            model_id=model_id or existing.model_id,
            reasoning_effort=reasoning_effort or existing.reasoning_effort,
            context_window_tokens=(
                context_window_tokens
                if context_window_tokens is not None
                else existing.context_window_tokens
            ),
        )

        self.directory.mkdir(parents=True, exist_ok=True)
        if existing_path is not None and existing_path.suffix == SESSION_FILE_SUFFIX:
            path = self._save_session_record(record, existing)
        else:
            path = self._write_session_record(record)
        self._update_index(record)
        return path

    def list(self, *, root: Path | str | None = None) -> builtins.list[SessionRecord]:
        """List session records."""
        self._prune_index_and_sessions()
        root_value = _normalize_root(root)
        entries = self._load_index().sessions.values()
        records = [
            entry.to_record()
            for entry in entries
            if root_value is None or entry.root == root_value
        ]
        return sorted(
            records,
            key=lambda record: record.updated_at or record.created_at or "",
            reverse=True,
        )

    def _session_path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError(
                "Session id must start with an alphanumeric character and "
                "use only letters, numbers, dot, underscore, or dash."
            )
        return self.directory / f"{session_id}{SESSION_FILE_SUFFIX}"

    def _legacy_session_path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError(
                "Session id must start with an alphanumeric character and "
                "use only letters, numbers, dot, underscore, or dash."
            )
        return self.directory / f"{session_id}{LEGACY_SESSION_FILE_SUFFIX}"

    def _existing_session_path(self, session_id: str) -> Path | None:
        path = self._session_path(session_id)
        if path.exists():
            return path
        legacy_path = self._legacy_session_path(session_id)
        if legacy_path.exists():
            return self._migrate_legacy_session_file(session_id)
        return None

    def _index_path(self) -> Path:
        return self.directory / SESSION_INDEX_NAME

    def _load_index(self) -> SessionIndex:
        path = self._index_path()
        if not path.exists():
            return SessionIndex()
        try:
            return SessionIndex.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return SessionIndex()

    def _update_index(self, record: SessionRecord) -> None:
        index = self._load_index()
        index.sessions[record.id] = SessionIndexEntry(
            id=record.id,
            root=record.root,
            title=record.title,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self._index_path().write_text(index.model_dump_json(indent=2), encoding="utf-8")

    def _prune_index_and_sessions(
        self, *, exclude_session_id: str | None = None
    ) -> None:
        index = self._load_index()
        cutoff = datetime.now(UTC) - timedelta(days=SESSION_RETENTION_DAYS)
        changed = False

        for session_id, entry in list(index.sessions.items()):
            if session_id == exclude_session_id:
                continue

            session_path = self._existing_session_path(session_id)
            legacy_path = self._legacy_session_path(session_id)
            if session_path is None or not session_path.exists():
                index.sessions.pop(session_id, None)
                changed = True
                continue

            last_activity = _parse_timestamp(entry.updated_at) or _parse_timestamp(
                entry.created_at
            )
            if last_activity is None or last_activity >= cutoff:
                continue

            try:
                session_path.unlink()
            except OSError:
                continue
            if legacy_path.exists():
                try:
                    legacy_path.unlink()
                except OSError:
                    pass
            index.sessions.pop(session_id, None)
            changed = True

        if changed:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._index_path().write_text(
                index.model_dump_json(indent=2), encoding="utf-8"
            )

    def _migrate_legacy_sessions(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob(f"*{SESSION_FILE_SUFFIX}"):
            session_id = path.stem
            if not SESSION_ID_PATTERN.fullmatch(session_id):
                continue
            self._migrate_snapshot_session_file(session_id)
        for path in self.directory.glob(f"*{LEGACY_SESSION_FILE_SUFFIX}"):
            if path.name == SESSION_INDEX_NAME or path.name.endswith(".queue.json"):
                continue
            session_id = path.stem
            if not SESSION_ID_PATTERN.fullmatch(session_id):
                continue
            target = self._session_path(session_id)
            if target.exists():
                continue
            self._migrate_legacy_session_file(session_id)

    def _migrate_legacy_session_file(self, session_id: str) -> Path:
        legacy_path = self._legacy_session_path(session_id)
        target_path = self._session_path(session_id)
        if not legacy_path.exists() or target_path.exists():
            return target_path if target_path.exists() else legacy_path
        raw_text = legacy_path.read_text(encoding="utf-8")
        record = self._decode_session_record(raw_text)
        if record.id != session_id:
            record = record.model_copy(update={"id": session_id})
        self._write_session_record(record)
        self._update_index(record)
        return target_path

    def _migrate_snapshot_session_file(self, session_id: str) -> Path:
        path = self._session_path(session_id)
        if not path.exists():
            return path
        raw_text = path.read_text(encoding="utf-8")
        if raw_text.lstrip().startswith('{"type":"session_stream"'):
            return path
        record = self._decode_session_record(raw_text)
        if record.id != session_id:
            record = record.model_copy(update={"id": session_id})
        self._write_session_record(record)
        self._update_index(record)
        return path

    def _decode_session_record(self, raw_text: str) -> SessionRecord:
        stripped = raw_text.lstrip()
        if stripped.startswith('{"type":"session_record"'):
            return SessionRecord.model_validate_json(
                self._json_object_from_jsonl(raw_text)
            )
        if stripped.startswith('{"type":"session_stream"'):
            return self._decode_session_event_stream(raw_text)
        if stripped.startswith("{"):
            try:
                return SessionRecord.model_validate_json(raw_text)
            except ValidationError:
                return self._decode_session_event_stream(raw_text)
        try:
            return SessionRecord.model_validate_json(
                self._json_object_from_jsonl(raw_text)
            )
        except (json.JSONDecodeError, ValidationError, ValueError):
            return self._decode_session_event_stream(raw_text)

    def _save_session_record(
        self,
        record: SessionRecord,
        existing: SessionRecord,
    ) -> Path:
        path = self._session_path(record.id)
        if self._can_append_session_record(record, existing):
            with path.open("a", encoding="utf-8") as file:
                file.write(self._metadata_jsonl(record))
                for entry in record.conversation_entries[
                    len(existing.conversation_entries) :
                ]:
                    file.write(self._entry_jsonl(entry))
            return path
        return self._write_session_record(record)

    def _can_append_session_record(
        self,
        record: SessionRecord,
        existing: SessionRecord,
    ) -> bool:
        existing_entry_count = len(existing.conversation_entries)
        if len(record.conversation_entries) < existing_entry_count:
            return False
        return (
            record.conversation_entries[:existing_entry_count]
            == existing.conversation_entries
        )

    def _write_session_record(self, record: SessionRecord) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._session_path(record.id)
        path.write_text(self._record_jsonl(record), encoding="utf-8")
        legacy_path = self._legacy_session_path(record.id)
        if legacy_path.exists():
            legacy_path.unlink()
        return path

    def _record_jsonl(self, record: SessionRecord) -> str:
        return (
            json.dumps(
                {"type": "session_stream", "version": 1},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
            + self._metadata_jsonl(record)
            + "".join(self._entry_jsonl(entry) for entry in record.conversation_entries)
        )

    def _metadata_jsonl(self, record: SessionRecord) -> str:
        payload = record.model_dump(
            mode="json",
            exclude={"conversation_entries"},
        )
        return (
            json.dumps(
                {"type": "session_metadata", "record": payload},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

    def _entry_jsonl(self, entry: ConversationEntry) -> str:
        return (
            json.dumps(
                {"type": "conversation_entry", "entry": entry.model_dump(mode="json")},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

    def _decode_session_event_stream(self, raw_text: str) -> SessionRecord:
        metadata: dict[str, object] = {}
        entries: list[ConversationEntry] = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                continue
            line_type = payload.get("type")
            if line_type == "session_stream":
                continue
            if line_type == "session_metadata" and isinstance(
                payload.get("record"), dict
            ):
                metadata.update(payload["record"])
                continue
            if line_type == "conversation_entry" and isinstance(
                payload.get("entry"), dict
            ):
                entries.append(ConversationEntry.model_validate(payload["entry"]))
                continue
            if "kind" in payload and "message" in payload:
                entries.append(ConversationEntry.model_validate(payload))
                continue
            metadata.update(payload)
        if not entries and not metadata:
            raise ValueError("No recoverable session events found.")
        metadata["conversation_entries"] = entries
        metadata.setdefault("id", "legacy-session")
        if entries and not metadata.get("leaf_id"):
            metadata["leaf_id"] = entries[-1].id
        return SessionRecord.model_validate(metadata)

    def _json_object_from_jsonl(self, raw_text: str) -> str:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Session file is empty.")
        if len(lines) == 1:
            return lines[0]
        return lines[-1]


def default_session_directory() -> Path:
    """Return the default directory used for CLI session records."""
    override = os.getenv("YOKE_SESSION_DIR")
    if override:
        return Path(override)
    return Path.home() / ".yoke" / "sessions"


def new_session_id() -> str:
    """Return a unique human-sortable session id."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def fallback_session_title(prompt: str) -> str:
    """Build a compact fallback title from the user's prompt."""
    title = " ".join(prompt.split()).strip()
    if not title:
        return "Untitled session"
    return title if len(title) <= 80 else title[:77].rstrip() + "..."


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_root(root: Path | str | None) -> str | None:
    if root is None:
        return None
    return str(Path(root).resolve())


def _normalize_title(title: str | None) -> str | None:
    if title is None:
        return None
    normalized = " ".join(title.split()).strip()
    return normalized or None
