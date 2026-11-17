"""Session persistence facade over the shared Yoke database."""

from __future__ import annotations

import builtins
import threading
import time
from collections.abc import Callable, Generator, Iterable, Sequence
from pathlib import Path

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill
from yoke.session.repository.helpers import last_user_message_at
from yoke.session.repository.models import SessionIndexEntry, SessionRecord
from yoke.session.repository.paths import (
    CURRENT_SESSION_SCHEMA_VERSION,
    SESSION_MAINTENANCE_INTERVAL_SECONDS,
    SESSION_RETENTION_DAYS,
    validate_session_id,
)
from yoke.session.repository.table import EntryTopology, SessionTable
from yoke.session.repository.tree import _resolve_saved_conversation_tree
from yoke.session.repository.tree_index import SessionTreeIndex
from yoke.session.repository.tree_writer import SessionTreeWriter
from yoke.session.repository.utils import (
    database_path_for,
    default_session_directory,
    fork_session_title,
    new_unique_session_id,
    normalize_root,
    normalize_title,
    timestamp,
)
from yoke.storage import Database, open_database


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


class SessionStore:
    """Session records stored in the shared SQLite database.

    ``directory`` still holds session state that is not in the database
    (workspace leases and disposable upload materializations). The database is chosen by
    :func:`database_path_for`, unless ``database_path`` is given. Queue,
    admission and event helpers take ``store.directory`` and resolve the
    database the same way, so pass ``database_path`` only for standalone
    access such as inspection tools.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        database_path: Path | None = None,
        migrate: bool = False,
    ) -> None:
        self.directory = (directory or default_session_directory()).resolve()
        self.database_path = database_path or database_path_for(self.directory)
        self.database: Database = open_database(self.database_path, migrate=migrate)
        self._table = SessionTable(self.database)
        self._tree_writer = SessionTreeWriter(self._table)
        self.save_tree_delta = self._tree_writer.save_tree_delta
        self.save_entry_metadata = self._tree_writer.save_entry_metadata
        self.save_indexed_tree_navigation = (
            self._tree_writer.save_indexed_tree_navigation
        )
        self.save_indexed_entry_metadata = self._tree_writer.save_indexed_entry_metadata
        self._maintenance_lock = threading.Lock()
        self._last_maintenance_at = float("-inf")

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record, or an empty record when it does not exist."""
        validate_session_id(session_id)
        record = self._table.load(session_id)
        if record is None:
            return SessionRecord(id=session_id)
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
        origin: str | None = None,
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
        """Save a session record, appending entries when the tree proves it."""
        validate_session_id(session_id)
        existing = existing_record or self.load(session_id)
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
                "origin": origin or existing.origin,
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
        record.stamp_tree_revision(
            self._table.write_record(
                record, existing=existing, appended=resolved.appended_entries
            )
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
        return record

    def list_index_entries(
        self,
        *,
        root: Path | str | None = None,
    ) -> builtins.list[SessionIndexEntry]:
        """List lightweight session summaries without loading conversation history."""
        entries = self._table.index_entries(normalize_root(root))
        return sorted(
            entries,
            key=lambda entry: (
                entry.pinned,
                entry.updated_at or entry.created_at or "",
            ),
            reverse=True,
        )

    def index_entry(self, session_id: str) -> SessionIndexEntry | None:
        """Return one session summary without loading conversation history."""
        return self._table.index_entry(session_id)

    def has_session_index(self) -> bool:
        """Return whether the database already exists (it always does once open)."""
        return self.database_path.exists()

    def summary_record(self, session_id: str) -> SessionRecord | None:
        """Return persisted metadata for one existing session, without history."""
        validate_session_id(session_id)
        return self._table.summary(session_id)

    def session_file_signature(self, session_id: str) -> tuple[int, int] | None:
        """Return (stored message bytes, row revision) for cache invalidation.

        The name predates the database. The revision changes on every write
        to the session or its entries; the byte count only grows on appends.
        """
        validate_session_id(session_id)
        return self._table.signature(session_id)

    def entry_topology(self, session_id: str) -> EntryTopology | None:
        """Return entry ids, parents and kinds in stored order, without payloads."""
        return self._table.topology(session_id)

    def read_entries(
        self,
        session_id: str,
        entry_ids: Sequence[str],
        *,
        raw_filter: Callable[[bytes], bytes] | None = None,
    ) -> builtins.list[ConversationEntry] | None:
        """Read selected entries in order; None if any id is not stored."""
        return self._table.read_entries(session_id, entry_ids, raw_filter=raw_filter)

    def iter_entries(self, session_id: str) -> Generator[ConversationEntry]:
        """Decode stored entries in order, one at a time, from one snapshot.

        Close the generator (or exhaust it) promptly: it holds a read
        transaction open on this thread until then.
        """
        return self._table.iter_entries(session_id)

    def insert_record(self, record: SessionRecord) -> bool:
        """Insert a complete record exactly as given; False if the id exists."""
        validate_session_id(record.id)
        return self._table.insert_record(record)

    def delete(self, session_id: str) -> bool:
        """Delete a session with its entries, events and inputs."""
        validate_session_id(session_id)
        return self._table.delete(session_id)

    def exists(self, session_id: str) -> bool:
        """Return whether a persisted session exists."""
        validate_session_id(session_id)
        return self._table.exists(session_id)

    def update_index(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """No-op kept for API compatibility: the session row is the index."""
        del record, entry_count
        return True

    def update_index_metadata(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        """No-op kept for API compatibility: the session row is the index."""
        del record, entry_count
        return True

    def update_metadata(
        self,
        record: SessionRecord,
        fields: Iterable[str],
        *,
        insert_if_missing: bool = False,
    ) -> SessionRecord:
        """Persist selected metadata fields from ``record``.

        A missing session raises ValueError unless creation is explicitly
        requested. Returns ``record`` stamped with the stored tree revision.
        """
        validate_session_id(record.id)
        tree_rev = self._table.update_metadata(
            record, fields, insert_if_missing=insert_if_missing
        )
        if tree_rev is None:
            raise ValueError(f"Session not found: {record.id}")
        record.stamp_tree_revision(tree_rev)
        return record

    def maintain_index(self, *, force: bool = False) -> None:
        """Apply session retention outside latency-sensitive requests."""
        now = time.monotonic()
        if not force and now - self._last_maintenance_at < (
            SESSION_MAINTENANCE_INTERVAL_SECONDS
        ):
            return
        with self._maintenance_lock:
            if not force and time.monotonic() - self._last_maintenance_at < (
                SESSION_MAINTENANCE_INTERVAL_SECONDS
            ):
                return
            self._table.prune(SESSION_RETENTION_DAYS)
            self.database.maintain()
            self._last_maintenance_at = time.monotonic()

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
        validate_session_id(source_session_id)
        fork_id = new_session_id_value or new_unique_session_id(self.exists)
        validate_session_id(fork_id)
        if self.exists(fork_id):
            raise ValueError(f"Session already exists: {fork_id}")
        summary = self.index_entry(source_session_id)
        source = self.summary_record(source_session_id)
        if (
            summary is None
            or source is None
            or (source.created_at is None and not summary.entry_count)
        ):
            raise ValueError(f"Session not found: {source_session_id}")
        now = timestamp()
        forked = source.model_copy(
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
            }
        )
        if not self._table.fork(source_session_id, forked):
            raise ValueError(f"Session not found: {source_session_id}")
        if materialize_result:
            return self.load(fork_id)
        summary = self.summary_record(fork_id)
        assert summary is not None
        return summary

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
        return self.update_metadata(updated, values)

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
