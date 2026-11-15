from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import Any

from yoke.agent.models import ConversationEntry
from yoke.agent.skills.models import ActiveSkill
from yoke.session.repository.helpers import last_user_message_at
from yoke.session.repository.models import SessionRecord
from yoke.session.repository.tree_index import SessionTreeIndex
from yoke.session.repository.utils import timestamp
from yoke.session.repository.writer import append_session_entry_metadata
from yoke.session.repository.writer import append_session_tree_delta


class _TreeOperations:
    def save_tree_delta(
        self: Any,
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
                "last_user_message_at": last_user_message_at(
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
        self.update_index(record)
        return record

    def save_entry_metadata(
        self: Any,
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
        self.update_index_metadata(record)
        return record

    def save_indexed_tree_navigation(
        self: Any,
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
            "last_user_message_at": last_user_message_at(
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
        self.update_index_metadata(record, entry_count=entry_count)
        return record

    def save_indexed_entry_metadata(
        self: Any,
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
        self.update_index_metadata(record)
        return record
