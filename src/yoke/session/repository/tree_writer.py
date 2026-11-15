"""Persist validated tree edits as compare-and-swap transactions."""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.skills.models import ActiveSkill
from yoke.session.repository.helpers import last_user_message_at
from yoke.session.repository.models import SessionRecord
from yoke.session.repository.rows import changed_fields
from yoke.session.repository.table import SessionTable
from yoke.session.repository.tree_index import SessionTreeIndex
from yoke.session.repository.utils import timestamp


class SessionTreeWriter:
    """Persist tree selections, appends and entry metadata edits.

    Each edit checks the stored tree revision the caller's record was read at
    (when known) inside its write transaction. A concurrent tree change by
    another writer raises ``SessionTreeConflict`` instead of being overwritten.
    """

    def __init__(self, table: SessionTable) -> None:
        self.table = table

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
                "last_user_message_at": last_user_message_at(
                    existing_record.last_user_message_at,
                    proven,
                ),
            }
        )
        record.stamp_tree_revision(
            self.table.append_tree(
                record,
                changed_fields(existing_record, record),
                proven,
                expected_tree_rev=existing_record.tree_revision,
                require_existing=False,
            )
        )
        if proven:
            record.conversation_entries.extend(proven)
        tree_index.commit_append(
            record.conversation_entries,
            leaf_id,
            proven,
        )
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
        record = existing_record.model_copy(
            update={"conversation_entries": entries, "updated_at": timestamp()}
        )
        record.stamp_tree_revision(
            self.table.replace_entry_metadata(
                record,
                entry,
                ["updated_at"],
                expected_tree_rev=existing_record.tree_revision,
                require_existing=False,
            )
        )
        tree_index.commit_entry_replacement(entries, entry)
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
        updates: dict[str, object] = {
            "leaf_id": leaf_id,
            "updated_at": timestamp(),
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
        record.stamp_tree_revision(
            self.table.append_tree(
                record,
                updates,
                appended_entries,
                expected_tree_rev=existing_record.tree_revision,
                require_existing=True,
            )
        )
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
        record = existing_record.model_copy(update={"updated_at": timestamp()})
        record.stamp_tree_revision(
            self.table.replace_entry_metadata(
                record,
                entry,
                ["updated_at"],
                expected_tree_rev=existing_record.tree_revision,
                require_existing=True,
            )
        )
        return record
