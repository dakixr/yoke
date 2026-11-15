"""Shared JSONL session persistence facade."""

from __future__ import annotations

import builtins
import re
from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.session.repository.index_service import SessionIndexService
from yoke.session.repository.index import session_file_ids
from yoke.session.repository.load import load_existing_record
from yoke.session.repository.load import reconcile_index_owned_metadata
from yoke.session.repository.load import scan_canonical_session_summary
from yoke.session.repository.models import SessionIndexEntry
from yoke.session.repository.helpers import last_user_message_at
from yoke.session.repository.tree_ops import _TreeOperations
from yoke.session.repository.fork_ops import _ForkOperations
from yoke.session.repository.metadata_ops import _MetadataOperations
from yoke.session.repository.models import SessionRecord
from yoke.session.repository.tree import _resolve_saved_conversation_tree
from yoke.session.repository.tree_index import SessionTreeIndex
from yoke.session.repository.utils import default_session_directory
from yoke.session.repository.utils import normalize_root
from yoke.session.repository.utils import normalize_title
from yoke.session.repository.utils import timestamp
from yoke.session.repository.writer import write_session_record

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_INDEX_NAME = "index.json"
SESSION_FILE_SUFFIX = ".jsonl"
SESSION_RETENTION_DAYS = 30
SESSION_MAINTENANCE_INTERVAL_SECONDS = 5.0
CURRENT_SESSION_SCHEMA_VERSION = 5


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


class SessionStore(_TreeOperations, _ForkOperations, _MetadataOperations):
    """JSONL-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()
        self._index_service = SessionIndexService(
            self.directory,
            index_path=self._index_path,
            session_path=self._session_path,
            load_summary=lambda session_id: self._load_summary_for_index(session_id),
            retention_days=SESSION_RETENTION_DAYS,
            session_file_suffix=SESSION_FILE_SUFFIX,
            session_id_pattern=SESSION_ID_PATTERN,
            list_session_ids=lambda directory: session_file_ids(
                directory,
                session_file_suffix=SESSION_FILE_SUFFIX,
                session_id_pattern=SESSION_ID_PATTERN,
            ),
        )
        self._index_cache = self._index_service._cache

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record."""
        path = self._session_path(session_id)
        if not path.exists():
            return SessionRecord(id=session_id)
        record = load_existing_record(
            self._index_service,
            session_id,
            path,
            current_schema_version=CURRENT_SESSION_SCHEMA_VERSION,
        )
        if record.version != CURRENT_SESSION_SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {record.version}.")
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
        reasoning_effort: str | None | _UnsetReasoningEffort = (
            _UNSET_REASONING_EFFORT
        ),
        context_window_tokens: int | None = None,
        existing_record: SessionRecord | None = None,
        tree_index: SessionTreeIndex | None = None,
    ) -> SessionRecord:
        """Save a session record."""
        existing_path = self._existing_session_path(session_id)
        existing = existing_record or (
            self.load(session_id)
            if existing_path is not None
            else SessionRecord(id=session_id)
        )
        if existing.id != session_id:
            raise ValueError("Existing session record id does not match save id.")
        now = timestamp()
        resolved = _resolve_saved_conversation_tree(
            existing,
            messages,
            conversation_entries=conversation_entries,
            leaf_id=leaf_id,
            tree_index=tree_index,
        )
        record = existing.model_copy(
            update={
                "id": session_id,
                "conversation_entries": resolved.entries,
                "leaf_id": resolved.leaf_id,
                "active_skills": list(
                    active_skills
                    if active_skills is not None
                    else existing.active_skills
                ),
                "skill_dirs": list(
                    skill_dirs if skill_dirs is not None else existing.skill_dirs
                ),
                "created_at": existing.created_at or now,
                "updated_at": now,
                "last_user_message_at": last_user_message_at(
                    existing.last_user_message_at,
                    (
                        resolved.appended_entries
                        if resolved.appended_entries is not None
                        else resolved.entries
                    ),
                ),
                "root": normalize_root(root) or existing.root,
                "title": normalize_title(title) or existing.title,
                "pinned": existing.pinned,
                "provider_name": provider_name or existing.provider_name,
                "model_id": model_id or existing.model_id,
                "reasoning_effort": (
                    existing.reasoning_effort
                    if isinstance(reasoning_effort, _UnsetReasoningEffort)
                    else reasoning_effort
                ),
                "context_window_tokens": (
                    context_window_tokens
                    if context_window_tokens is not None
                    else existing.context_window_tokens
                ),
            }
        )

        self.directory.mkdir(parents=True, exist_ok=True)
        self._write_session_record(
            record,
            existing_record=existing,
            trusted_append_entries=resolved.appended_entries,
        )
        if resolved.appended_entries:
            record.conversation_entries.extend(resolved.appended_entries)
        if tree_index is not None:
            if resolved.appended_entries is None:
                tree_index.replace(record.conversation_entries, record.leaf_id)
            else:
                tree_index.commit_append(
                    record.conversation_entries,
                    record.leaf_id,
                    resolved.appended_entries,
                )
        self._index_service.update(record)
        return record

    def list(self, *, root: Path | str | None = None) -> builtins.list[SessionRecord]:
        """List session records."""
        entries = self.list_index_entries(root=root)
        records = [entry.to_record() for entry in entries]
        return records

    def list_index_entries(
        self,
        *,
        root: Path | str | None = None,
        maintain: bool = True,
    ) -> builtins.list[SessionIndexEntry]:
        """List lightweight session summaries without loading conversation history."""
        if maintain:
            self._maintain_index_if_due()
        root_value = normalize_root(root)
        entries = [
            entry
            for entry in self._index_service.read().sessions.values()
            if root_value is None or entry.root == root_value
        ]
        return sorted(
            entries,
            key=lambda entry: (
                entry.pinned,
                entry.updated_at or entry.created_at or "",
            ),
            reverse=True,
        )

    def index_entry(self, session_id: str) -> SessionIndexEntry | None:
        """Return one cached session summary without loading conversation history."""
        return self._index_service.entry(session_id)

    def has_session_index(self) -> bool:
        """Return whether this store already has an index snapshot on disk."""
        return self._index_path().exists()

    def summary_record(self, session_id: str) -> SessionRecord | None:
        """Return lightweight persisted metadata for one existing session.

        The normal case is index-only. A missing index entry falls back to a
        complete load so exact-session APIs remain correct while background
        maintenance repairs old or externally modified stores.
        """
        if not self.exists(session_id):
            return None
        entry = self.index_entry(session_id)
        if entry is not None:
            return entry.to_record()
        return self.load(session_id)

    def session_file_signature(self, session_id: str) -> tuple[int, int] | None:
        """Return size and mtime for cache invalidation without reading the file."""
        path = self._existing_session_path(session_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def exists(self, session_id: str) -> bool:
        """Return whether a persisted session exists."""
        return self._existing_session_path(session_id) is not None

    def _session_path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError(
                "Session id must start with an alphanumeric character and "
                "use only letters, numbers, dot, underscore, or dash."
            )
        return self.directory / f"{session_id}{SESSION_FILE_SUFFIX}"

    def session_path(self, session_id: str) -> Path:
        """Return the validated path for one persisted session."""
        return self._session_path(session_id)

    def write_session_record(
        self,
        record: SessionRecord,
        *,
        existing_record: SessionRecord | None = None,
        trusted_append_entries: tuple[ConversationEntry, ...] | None = None,
    ) -> Path:
        """Write one session record through the repository writer."""
        return self._write_session_record(
            record,
            existing_record=existing_record,
            trusted_append_entries=trusted_append_entries,
        )

    def update_index(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """Update the durable session summary index."""
        return self._index_service.update(record, entry_count=entry_count)

    def update_index_metadata(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """Update index metadata while retaining its known entry count."""
        return self._index_service.update_metadata(record, entry_count=entry_count)

    def _update_index(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """Compatibility hook for existing internal persistence tests."""
        return self.update_index(record, entry_count=entry_count)

    def _update_index_metadata(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """Compatibility hook for existing internal persistence tests."""
        return self.update_index_metadata(record, entry_count=entry_count)

    def _existing_session_path(self, session_id: str) -> Path | None:
        path = self._session_path(session_id)
        return path if path.exists() else None

    def _index_path(self) -> Path:
        return self.directory / SESSION_INDEX_NAME

    def _load_summary_for_index(self, session_id: str) -> tuple[SessionRecord, int]:
        path = self._session_path(session_id)
        scanned = scan_canonical_session_summary(path, session_id)
        if scanned is not None:
            record, entry_count = scanned
            return (
                reconcile_index_owned_metadata(record, self.index_entry(session_id)),
                entry_count,
            )
        record = load_existing_record(
            self._index_service,
            session_id,
            path,
            current_schema_version=CURRENT_SESSION_SCHEMA_VERSION,
            update_index=False,
        )
        if record.version != CURRENT_SESSION_SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {record.version}.")
        return record, len(record.conversation_entries)

    def maintain_index(self, *, force: bool = False) -> None:
        """Repair and prune the session index outside latency-sensitive requests."""
        self._index_service.maintain(force=force)

    def _maintain_index_if_due(self) -> None:
        """Run throttled index maintenance for list-style callers."""
        self._index_service.maintain_if_due()

    def _prune_index_and_sessions(self) -> bool:
        """Compatibility hook for maintenance tests and internal callers."""
        return self._index_service.prune()

    def _write_session_record(
        self,
        record: SessionRecord,
        *,
        existing_record: SessionRecord | None = None,
        trusted_append_entries: tuple[ConversationEntry, ...] | None = None,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return write_session_record(
            record,
            path=self._session_path(record.id),
            existing_record=existing_record,
            trusted_append_entries=trusted_append_entries,
        )
