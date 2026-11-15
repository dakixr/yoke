"""Shared JSONL session persistence facade."""

from __future__ import annotations

import builtins
from pathlib import Path

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill
from yoke.session.repository.helpers import last_user_message_at
from yoke.session.repository.index import SESSION_INDEX_SUMMARY_VERSION
from yoke.session.repository.index_service import SessionIndexService
from yoke.session.repository.load import load_existing_record
from yoke.session.repository.models import SessionIndexEntry, SessionRecord
from yoke.session.repository.paths import (
    CURRENT_SESSION_SCHEMA_VERSION,
    SESSION_INDEX_NAME,
    session_path,
)
from yoke.session.repository.tree import _resolve_saved_conversation_tree
from yoke.session.repository.tree_index import SessionTreeIndex
from yoke.session.repository.tree_writer import SessionTreeWriter
from yoke.session.repository.utils import (
    default_session_directory,
    fork_session_title,
    new_unique_session_id,
    normalize_root,
    normalize_title,
    timestamp,
)
from yoke.session.repository.writer import (
    append_session_metadata,
    clone_canonical_session,
    write_session_record,
)


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


class SessionStore:
    """JSONL-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()
        self._index_service = SessionIndexService(self.directory)
        self._tree_writer = SessionTreeWriter(self.directory, self._index_service)
        self.save_tree_delta = self._tree_writer.save_tree_delta
        self.save_entry_metadata = self._tree_writer.save_entry_metadata
        self.save_indexed_tree_navigation = (
            self._tree_writer.save_indexed_tree_navigation
        )
        self.save_indexed_entry_metadata = self._tree_writer.save_indexed_entry_metadata

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record."""
        path = self.session_path(session_id)
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
        self.write_session_record(
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
            self._index_service.maintain_if_due()
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

    def session_path(self, session_id: str) -> Path:
        """Return the validated path for one persisted session."""
        return session_path(self.directory, session_id)

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

    def _existing_session_path(self, session_id: str) -> Path | None:
        path = self.session_path(session_id)
        return path if path.exists() else None

    def _index_path(self) -> Path:
        return self.directory / SESSION_INDEX_NAME

    def maintain_index(self, *, force: bool = False) -> None:
        """Repair and prune the session index outside latency-sensitive requests."""
        self._index_service.maintain(force=force)

    def write_session_record(
        self,
        record: SessionRecord,
        *,
        existing_record: SessionRecord | None = None,
        trusted_append_entries: tuple[ConversationEntry, ...] | None = None,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return write_session_record(
            record,
            path=self.session_path(record.id),
            existing_record=existing_record,
            trusted_append_entries=trusted_append_entries,
        )

    def fork(
        self,
        source_session_id: str,
        *,
        new_session_id_value: str | None = None,
        root: Path | str | None = None,
        title: str | None = None,
        selected_leaf_id: str | None = None,
        materialize_result: bool = True,
    ) -> SessionRecord:
        """Create a persisted copy of an existing session under a new id."""
        fork_id = new_session_id_value or new_unique_session_id(self.exists)
        if self.exists(fork_id):
            raise ValueError(f"Session already exists: {fork_id}")
        source_index = self.index_entry(source_session_id)
        source_signature = self.session_file_signature(source_session_id)
        if (
            source_index is not None
            and source_index.summary_version == SESSION_INDEX_SUMMARY_VERSION
            and source_index.entry_count is not None
            and source_signature is not None
            and source_index.file_size == source_signature[0]
            and source_index.file_mtime_ns == source_signature[1]
        ):
            now = timestamp()
            fork_root = normalize_root(root) or source_index.root
            fork_title = normalize_title(title) or fork_session_title(
                source_index.title
            )
            fork_leaf = (
                selected_leaf_id
                if selected_leaf_id is not None
                else source_index.leaf_id
            )
            forked = source_index.to_record().model_copy(
                update={
                    "id": fork_id,
                    "created_at": now,
                    "updated_at": now,
                    "root": fork_root,
                    "title": fork_title,
                    "pinned": False,
                    "archived_at": None,
                    "context_usage": None,
                    "leaf_id": fork_leaf,
                }
            )
            if clone_canonical_session(
                self.session_path(source_session_id),
                self.session_path(fork_id),
                metadata_changes={
                    "id": fork_id,
                    "created_at": now,
                    "updated_at": now,
                    "root": fork_root,
                    "title": fork_title,
                    "pinned": False,
                    "archived_at": None,
                    "context_usage": None,
                    "leaf_id": fork_leaf,
                },
            ):
                self.update_index(forked, entry_count=source_index.entry_count)
                return self.load(fork_id) if materialize_result else forked

        source = self.load(source_session_id)
        if source.created_at is None and not source.conversation_entries:
            raise ValueError(f"Session not found: {source_session_id}")
        now = timestamp()
        forked = source.model_copy(
            deep=True,
            update={
                "id": fork_id,
                "created_at": now,
                "updated_at": now,
                "root": normalize_root(root) or source.root,
                "title": normalize_title(title) or fork_session_title(source.title),
                "pinned": False,
                "archived_at": None,
                "context_usage": None,
                "leaf_id": (
                    selected_leaf_id if selected_leaf_id is not None else source.leaf_id
                ),
            },
        )
        self.write_session_record(forked)
        self.update_index(forked)
        return forked

    def _patch_metadata(
        self,
        session_id: str,
        values: dict[str, object],
        *,
        existing_record: SessionRecord | None = None,
        error_name: str = "session",
    ) -> SessionRecord:
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError(
                f"Existing session record id does not match {error_name} id."
            )
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        updated = record.model_copy(update=values)
        path = self.session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self.write_session_record(updated)
        self._index_service.update_metadata(updated)
        return updated

    def set_pinned(
        self,
        session_id: str,
        pinned: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {"pinned": pinned},
            existing_record=existing_record,
            error_name="pin",
        )

    def set_title(
        self,
        session_id: str,
        title: str | None,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {"title": normalize_title(title), "updated_at": timestamp()},
            existing_record=existing_record,
            error_name="title",
        )

    def set_archived(
        self,
        session_id: str,
        archived: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        now = timestamp()
        return self._patch_metadata(
            session_id,
            {"archived_at": now if archived else None, "updated_at": now},
            existing_record=existing_record,
            error_name="archive",
        )

    def set_selection(
        self,
        session_id: str,
        *,
        provider_name: str | None,
        model_id: str | None,
        reasoning_effort: str | None,
        context_window_tokens: int | None,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {
                "provider_name": provider_name,
                "model_id": model_id,
                "reasoning_effort": reasoning_effort,
                "context_window_tokens": context_window_tokens,
                "context_usage": None,
                "updated_at": timestamp(),
            },
            existing_record=existing_record,
            error_name="selection",
        )

    def set_context_usage(
        self,
        session_id: str,
        usage: dict[str, object],
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        record = existing_record or self.summary_record(session_id)
        return self._patch_metadata(
            session_id,
            {"context_usage": dict(usage)},
            existing_record=record,
        )
