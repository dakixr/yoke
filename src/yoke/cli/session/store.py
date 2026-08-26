"""CLI-owned JSONL session persistence."""

from __future__ import annotations

import builtins
from collections.abc import Sequence
import re
from pathlib import Path

from pydantic import ValidationError

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.session.index import repair_index_from_session_files
from yoke.cli.session.load import load_existing_record
from yoke.cli.session.maintenance import prune_index_and_sessions
from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.tree import _resolve_saved_conversation_tree
from yoke.cli.session.tree_index import SessionTreeIndex
from yoke.cli.session.writer import append_session_metadata
from yoke.cli.session.writer import append_session_entry_metadata
from yoke.cli.session.utils import default_session_directory
from yoke.cli.session.utils import fork_session_title
from yoke.cli.session.utils import new_unique_session_id
from yoke.cli.session.utils import normalize_root
from yoke.cli.session.utils import normalize_title
from yoke.cli.session.utils import timestamp
from yoke.cli.session.writer import write_session_record

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_INDEX_NAME = "index.json"
SESSION_FILE_SUFFIX = ".jsonl"
SESSION_RETENTION_DAYS = 30
CURRENT_SESSION_SCHEMA_VERSION = 5


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


class SessionStore:
    """JSONL-backed store for CLI session records."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_session_directory()).resolve()

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
                "updated_at": timestamp(),
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
        self._update_index(record)
        return record

    def list(self, *, root: Path | str | None = None) -> builtins.list[SessionRecord]:
        """List session records."""
        self._repair_index_from_session_files()
        self._prune_index_and_sessions()
        root_value = normalize_root(root)
        entries = self._load_index().sessions.values()
        records = [
            entry.to_record()
            for entry in entries
            if root_value is None or entry.root == root_value
        ]
        return sorted(
            records,
            key=lambda record: (
                record.pinned,
                record.updated_at or record.created_at or "",
            ),
            reverse=True,
        )

    def fork(
        self,
        source_session_id: str,
        *,
        new_session_id_value: str | None = None,
        root: Path | str | None = None,
        title: str | None = None,
    ) -> SessionRecord:
        """Create a persisted copy of an existing session under a new id."""
        source = self.load(source_session_id)
        if source.created_at is None and not source.conversation_entries:
            raise ValueError(f"Session not found: {source_session_id}")
        fork_id = new_session_id_value or new_unique_session_id(self.exists)
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
        self._update_index(record)
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
        self._update_index(record)
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
        self._update_index(record)
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
            "updated_at": now,
        }
        record = record.model_copy(update=values)
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self._write_session_record(record)
        self._update_index(record)
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
            pinned=record.pinned,
            archived_at=record.archived_at,
        )
        self._index_path().write_text(index.model_dump_json(indent=2), encoding="utf-8")

    def _repair_index_from_session_files(self) -> None:
        index = self._load_index()
        if repair_index_from_session_files(
            directory=self.directory,
            index=index,
            session_file_suffix=SESSION_FILE_SUFFIX,
            session_id_pattern=SESSION_ID_PATTERN,
            existing_session_path=self._existing_session_path,
        ):
            self.directory.mkdir(parents=True, exist_ok=True)
            self._index_path().write_text(
                index.model_dump_json(indent=2), encoding="utf-8"
            )

    def _prune_index_and_sessions(
        self, *, exclude_session_id: str | None = None
    ) -> None:
        prune_index_and_sessions(
            self,
            retention_days=SESSION_RETENTION_DAYS,
            exclude_session_id=exclude_session_id,
        )

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
