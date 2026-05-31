"""CLI-owned JSON session persistence."""

from __future__ import annotations

import builtins
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
    """JSON-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record."""
        path = self._session_path(session_id)
        if not path.exists():
            return SessionRecord(id=session_id)
        try:
            raw_text = path.read_text(encoding="utf-8")
            record = SessionRecord.model_validate_json(raw_text)
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
        ):
            record = record.model_copy(
                update={
                    "version": 4,
                    "conversation_entries": migrated_entries,
                    "leaf_id": leaf_id,
                }
            )
            path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
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
        existing = (
            self.load(session_id) if path.exists() else SessionRecord(id=session_id)
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
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
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
        return self.directory / f"{session_id}.json"

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

            session_path = self._session_path(session_id)
            if not session_path.exists():
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
            index.sessions.pop(session_id, None)
            changed = True

        if changed:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._index_path().write_text(
                index.model_dump_json(indent=2), encoding="utf-8"
            )


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
