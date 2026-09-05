"""CLI-owned JSONL session persistence."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable
from collections.abc import Sequence
import re
from pathlib import Path
from threading import Lock
from threading import RLock
import time

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.session.index import SESSION_INDEX_SUMMARY_VERSION
from yoke.cli.session.index import repair_index_from_session_files
from yoke.cli.session.index import session_file_ids
from yoke.cli.session.index import session_index_entry
from yoke.cli.session.index_cache import SessionIndexCache
from yoke.cli.session.load import load_existing_record
from yoke.cli.session.load import reconcile_index_owned_metadata
from yoke.cli.session.load import scan_canonical_session_summary
from yoke.cli.session.maintenance import prune_index_and_sessions
from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.tree import _resolve_saved_conversation_tree
from yoke.cli.session.tree_index import SessionTreeIndex
from yoke.cli.session.writer import append_session_metadata
from yoke.cli.session.writer import append_session_entry_metadata
from yoke.cli.session.writer import append_session_tree_delta
from yoke.cli.session.writer import clone_canonical_session
from yoke.cli.session.utils import default_session_directory
from yoke.cli.session.utils import fork_session_title
from yoke.cli.session.utils import new_unique_session_id
from yoke.cli.session.utils import normalize_root
from yoke.cli.session.utils import normalize_title
from yoke.cli.session.utils import parse_timestamp
from yoke.cli.session.utils import timestamp
from yoke.cli.session.writer import write_session_record

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_INDEX_NAME = "index.json"
SESSION_FILE_SUFFIX = ".jsonl"
SESSION_RETENTION_DAYS = 30
SESSION_MAINTENANCE_INTERVAL_SECONDS = 5.0
CURRENT_SESSION_SCHEMA_VERSION = 5

logger = logging.getLogger(__name__)


def _last_user_message_at(
    current: str | None,
    entries: Sequence[ConversationEntry],
) -> str | None:
    latest = current
    latest_at = parse_timestamp(current)
    for entry in entries:
        if entry.kind != "user":
            continue
        created_at = parse_timestamp(entry.created_at)
        if created_at is None:
            continue
        if latest_at is None or created_at > latest_at:
            latest = entry.created_at
            latest_at = created_at
    return latest


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


class SessionStore:
    """JSONL-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()
        self._maintenance_lock = Lock()
        self._index_lock = RLock()
        self._last_maintenance_at = float("-inf")
        self._index_cache = SessionIndexCache(self._index_path())

    def load(self, session_id: str) -> SessionRecord:
        """Load a session record."""
        path = self._session_path(session_id)
        if not path.exists():
            return SessionRecord(id=session_id)
        record = load_existing_record(
            self,
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
                "last_user_message_at": _last_user_message_at(
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
        self._update_index(record)
        return record

    def save_tree_delta(
        self,
        session_id: str,
        *,
        existing_record: SessionRecord,
        tree_index: SessionTreeIndex,
        leaf_id: str,
        appended_entries: Sequence[ConversationEntry] = (),
    ) -> SessionRecord:
        """Commit a validated tree selection and optional append suffix."""
        if existing_record.id != session_id:
            raise ValueError("Existing session record id does not match save id.")
        proven = tree_index.prove_append(appended_entries, leaf_id)
        if proven is None:
            tree_index.replace(
                existing_record.conversation_entries,
                existing_record.leaf_id,
            )
            proven = tree_index.prove_append(appended_entries, leaf_id)
            if proven is None:
                raise ValueError("Session tree delta is not valid for this session.")
        record = existing_record.model_copy(
            update={
                "conversation_entries": existing_record.conversation_entries,
                "leaf_id": leaf_id,
                "context_usage": None,
                "updated_at": timestamp(),
                "last_user_message_at": _last_user_message_at(
                    existing_record.last_user_message_at,
                    proven,
                ),
            }
        )
        self._write_session_record(
            record,
            existing_record=existing_record,
            trusted_append_entries=proven,
        )
        if proven:
            record.conversation_entries.extend(proven)
        tree_index.commit_append(
            record.conversation_entries,
            leaf_id,
            proven,
        )
        self._update_index(record)
        return record

    def save_entry_metadata(
        self,
        session_id: str,
        entry: ConversationEntry,
        *,
        existing_record: SessionRecord,
        tree_index: SessionTreeIndex,
    ) -> SessionRecord:
        """Commit one copy-on-write entry metadata replacement."""
        if existing_record.id != session_id:
            raise ValueError("Existing session record id does not match save id.")
        position = tree_index.entry_position(entry.id)
        existing_entry = tree_index.entry_ref(entry.id)
        if existing_entry.parent_id != entry.parent_id:
            raise ValueError("Entry metadata updates cannot change topology.")
        entries = list(existing_record.conversation_entries)
        if entries[position].id != entry.id:
            tree_index.replace(entries, existing_record.leaf_id)
            position = tree_index.entry_position(entry.id)
        entries[position] = entry
        now = timestamp()
        record = existing_record.model_copy(
            update={
                "conversation_entries": entries,
                "updated_at": now,
            }
        )
        path = self._session_path(session_id)
        if path.exists():
            append_session_entry_metadata(
                path,
                entry_id=entry.id,
                entry_metadata=entry.metadata,
                session_changes={"updated_at": now},
            )
        else:
            self._write_session_record(record)
        tree_index.commit_entry_replacement(entries, entry)
        self._update_index_metadata(record)
        return record

    def save_indexed_tree_navigation(
        self,
        session_id: str,
        *,
        existing_record: SessionRecord,
        leaf_id: str,
        appended_entries: tuple[ConversationEntry, ...] = (),
        active_skills: builtins.list[ActiveSkill] | None = None,
        clear_context_usage: bool = True,
    ) -> SessionRecord:
        """Persist a topology-proven tree checkout without loading old entries."""
        if existing_record.id != session_id:
            raise ValueError("Existing session record id does not match save id.")
        path = self._session_path(session_id)
        if not path.exists():
            raise ValueError(f"Session not found: {session_id}")
        now = timestamp()
        updates: dict[str, object] = {
            "leaf_id": leaf_id,
            "updated_at": now,
            "last_user_message_at": _last_user_message_at(
                existing_record.last_user_message_at,
                appended_entries,
            ),
        }
        if clear_context_usage:
            updates["context_usage"] = None
        if active_skills is not None:
            updates["active_skills"] = list(active_skills)
        record = existing_record.model_copy(update=updates)
        session_changes: dict[str, object] = {
            "leaf_id": leaf_id,
            "updated_at": now,
            "last_user_message_at": record.last_user_message_at,
        }
        if clear_context_usage:
            session_changes["context_usage"] = None
        if active_skills is not None:
            session_changes["active_skills"] = [
                skill.model_dump(mode="json") for skill in active_skills
            ]
        append_session_tree_delta(
            path,
            session_changes=session_changes,
            appended_entries=appended_entries,
        )
        existing_index = self.index_entry(session_id)
        entry_count = (
            (existing_index.entry_count or 0) + len(appended_entries)
            if existing_index is not None and existing_index.entry_count is not None
            else len(existing_record.conversation_entries) + len(appended_entries)
        )
        self._update_index_metadata(record, entry_count=entry_count)
        return record

    def save_indexed_entry_metadata(
        self,
        session_id: str,
        entry: ConversationEntry,
        *,
        existing_record: SessionRecord,
    ) -> SessionRecord:
        """Persist one proven entry metadata update without loading the tree."""
        if existing_record.id != session_id:
            raise ValueError("Existing session record id does not match save id.")
        path = self._session_path(session_id)
        if not path.exists():
            raise ValueError(f"Session not found: {session_id}")
        now = timestamp()
        record = existing_record.model_copy(update={"updated_at": now})
        append_session_entry_metadata(
            path,
            entry_id=entry.id,
            entry_metadata=entry.metadata,
            session_changes={"updated_at": now},
        )
        self._update_index_metadata(record)
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
            for entry in self._index_cache.read().sessions.values()
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
        return self._index_cache.read().sessions.get(session_id)

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
                self._session_path(source_session_id),
                self._session_path(fork_id),
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
                self._update_index(forked, entry_count=source_index.entry_count)
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
        self._write_session_record(forked)
        self._update_index(forked)
        return forked

    def exists(self, session_id: str) -> bool:
        """Return whether a persisted session exists."""
        return self._existing_session_path(session_id) is not None

    def set_pinned(
        self,
        session_id: str,
        pinned: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        """Persist whether a session should be pinned in selectors."""
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError("Existing session record id does not match pin id.")
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        record = record.model_copy(update={"pinned": pinned})
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, {"pinned": pinned})
        else:
            self._write_session_record(record)
        self._update_index_metadata(record)
        return record

    def set_title(
        self,
        session_id: str,
        title: str | None,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        """Persist the normalized title for one existing session."""
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError("Existing session record id does not match title id.")
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        normalized = normalize_title(title)
        now = timestamp()
        record = record.model_copy(
            update={
                "title": normalized,
                "updated_at": now,
            }
        )
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(
                path,
                {
                    "title": normalized,
                    "updated_at": now,
                },
            )
        else:
            self._write_session_record(record)
        self._update_index_metadata(record)
        return record

    def set_archived(
        self,
        session_id: str,
        archived: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        """Persist whether a session is archived from the active work list."""
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError("Existing session record id does not match archive id.")
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        now = timestamp()
        values: dict[str, object] = {
            "archived_at": now if archived else None,
            "updated_at": now,
        }
        record = record.model_copy(update=values)
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self._write_session_record(record)
        self._update_index_metadata(record)
        return record

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
        """Persist provider and model selection without rewriting conversation state."""
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError("Existing session record id does not match selection id.")
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        now = timestamp()
        values: dict[str, object] = {
            "provider_name": provider_name,
            "model_id": model_id,
            "reasoning_effort": reasoning_effort,
            "context_window_tokens": context_window_tokens,
            "context_usage": None,
            "updated_at": now,
        }
        record = record.model_copy(update=values)
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self._write_session_record(record)
        self._update_index_metadata(record)
        return record

    def set_context_usage(
        self,
        session_id: str,
        usage: dict[str, object],
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        """Persist the latest safe context-usage snapshot without loading history."""
        record = existing_record or self.summary_record(session_id)
        if record is None or record.id != session_id or record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        values: dict[str, object] = {"context_usage": dict(usage)}
        record = record.model_copy(update=values)
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self._write_session_record(record)
        self._update_index_metadata(record)
        return record

    def _session_path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError(
                "Session id must start with an alphanumeric character and "
                "use only letters, numbers, dot, underscore, or dash."
            )
        return self.directory / f"{session_id}{SESSION_FILE_SUFFIX}"

    def _existing_session_path(self, session_id: str) -> Path | None:
        path = self._session_path(session_id)
        return path if path.exists() else None

    def _index_path(self) -> Path:
        return self.directory / SESSION_INDEX_NAME

    def _update_index(
        self,
        record: SessionRecord,
        *,
        entry_count: int | None = None,
    ) -> None:
        def mutate(index: SessionIndex) -> bool:
            index.sessions[record.id] = session_index_entry(
                record,
                path=self._session_path(record.id),
                entry_count=entry_count,
            )
            return True

        self._mutate_index(mutate)

    def _update_index_metadata(
        self,
        record: SessionRecord,
        *,
        entry_count: int | None = None,
    ) -> None:
        """Update metadata without requiring a materialized conversation list."""

        def mutate(index: SessionIndex) -> bool:
            existing = index.sessions.get(record.id)
            index.sessions[record.id] = session_index_entry(
                record,
                path=self._session_path(record.id),
                entry_count=(
                    entry_count
                    if entry_count is not None
                    else existing.entry_count
                    if existing is not None and existing.entry_count is not None
                    else len(record.conversation_entries)
                ),
            )
            return True

        self._mutate_index(mutate)

    def _repair_index_from_session_files(self) -> bool:
        def mutate(index: SessionIndex) -> bool:
            return repair_index_from_session_files(
                directory=self.directory,
                index=index,
                session_file_suffix=SESSION_FILE_SUFFIX,
                session_id_pattern=SESSION_ID_PATTERN,
                load_summary=self._load_summary_for_index,
            )

        return self._mutate_index(mutate)

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
            self,
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
        if not force:
            self._maintain_index_if_due()
            return
        with self._maintenance_lock:
            repaired = self._repair_index_from_session_files()
            pruned = self._prune_index_and_sessions()
            if repaired and pruned:
                self._last_maintenance_at = time.monotonic()

    def _maintain_index_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance_at < SESSION_MAINTENANCE_INTERVAL_SECONDS:
            return
        with self._maintenance_lock:
            now = time.monotonic()
            if now - self._last_maintenance_at < SESSION_MAINTENANCE_INTERVAL_SECONDS:
                return
            repaired = self._repair_index_from_session_files()
            pruned = self._prune_index_and_sessions()
            if repaired and pruned:
                self._last_maintenance_at = time.monotonic()

    def _mutate_index(self, mutator: Callable[[SessionIndex], bool]) -> bool:
        """Best-effort cache update after the authoritative JSONL write."""
        with self._index_lock:
            try:
                self._index_cache.update(mutator)
                return True
            except OSError:
                self._last_maintenance_at = float("-inf")
                logger.warning(
                    "Session index update failed; durable session files will repair it.",
                    exc_info=True,
                )
                return False

    def _prune_index_and_sessions(
        self, *, exclude_session_id: str | None = None
    ) -> bool:
        def mutate(index: SessionIndex) -> bool:
            existing_session_ids = session_file_ids(
                self.directory,
                session_file_suffix=SESSION_FILE_SUFFIX,
                session_id_pattern=SESSION_ID_PATTERN,
            )
            return prune_index_and_sessions(
                self,
                index=index,
                retention_days=SESSION_RETENTION_DAYS,
                exclude_session_id=exclude_session_id,
                existing_session_ids=existing_session_ids,
            )

        return self._mutate_index(mutate)

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
