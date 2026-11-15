"""Canonical tree intents with indexed and legacy delta persistence."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import NavigationOutcome
from yoke.http.errors import ApiError
from yoke.http.models.session import TreeEntryInfo
from yoke.http.services.projectors import project_tree, project_tree_entry
from yoke.session import SessionRecord, SessionStore, SessionTreeIndex


@dataclass
class SessionTreeEdit:
    store: SessionStore
    record: SessionRecord
    tree: SessionTree
    target_id: str
    indexed_storage: bool
    baseline: int
    state: tuple[ConversationEntry, bool, bool, int] | None = None

    @classmethod
    def indexed(
        cls,
        store: SessionStore,
        record: SessionRecord,
        target: ConversationEntry,
        state: tuple[ConversationEntry, bool, bool, int] | None,
    ) -> SessionTreeEdit:
        # Navigation without abandoned-path rendering needs only the selected
        # entry, its parent identity, and the previous leaf identity. Payloads
        # outside this intent stay in storage. Only newly appended entries are saved.
        entries: list[ConversationEntry] = []
        ids = {target.id}
        for entry_id in (target.parent_id, record.leaf_id):
            if entry_id is not None and entry_id not in ids:
                entries.append(ConversationEntry(kind="instruction", id=entry_id))
                ids.add(entry_id)
        entries.append(target.model_copy(deep=True))
        tree = SessionTree.borrow_validated(entries, record.leaf_id)
        return cls(store, record, tree, target.id, True, len(entries), state)

    @classmethod
    def legacy(
        cls, store: SessionStore, record: SessionRecord, target_id: str
    ) -> SessionTreeEdit:
        tree = SessionTree.restore(record.conversation_entries, record.leaf_id)
        try:
            tree.ref_from_persisted_id(target_id)
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        return cls(
            store, record, tree, target_id, False, len(record.conversation_entries)
        )

    def navigate(
        self, branch_summary: str | None
    ) -> tuple[SessionRecord, NavigationOutcome]:
        target = self.tree.ref_from_persisted_id(self.target_id)
        selected = self.tree.export_entry_for_persistence(self.target_id)
        if (
            self.indexed_storage
            and selected.id != self.record.leaf_id
            and selected.kind == "user"
            and selected.message is not None
            and selected.parent_id is None
        ):
            raise ApiError(
                409,
                "tree_root_navigation_unsupported",
                "This legacy tree has no persisted parent before the selected user entry.",
            )
        outcome = self.tree.navigate(target, branch_summary=branch_summary)
        if self.tree.leaf_id is None:
            raise ApiError(
                409,
                "tree_root_navigation_unsupported",
                "This legacy tree has no persisted parent before the selected user entry.",
            )
        if self.indexed_storage and self.target_id == self.record.leaf_id:
            return self.record, outcome
        exported = self.tree.export_for_persistence()
        appended = exported.entries[self.baseline :]
        if self.indexed_storage:
            updated = self.store.save_indexed_tree_navigation(
                self.record.id,
                existing_record=self.record,
                leaf_id=self.tree.leaf_id,
                appended_entries=appended,
            )
        else:
            updated = self.store.save_tree_delta(
                self.record.id,
                existing_record=self.record,
                tree_index=SessionTreeIndex(
                    self.record.conversation_entries, self.record.leaf_id
                ),
                leaf_id=self.tree.leaf_id,
                appended_entries=list(appended),
            )
        return updated, outcome

    def label(self, value: str | None) -> tuple[SessionRecord, TreeEntryInfo]:
        self.tree.set_label(self.tree.ref_from_persisted_id(self.target_id), value)
        entry = self.tree.export_entry_for_persistence(self.target_id)
        if self.indexed_storage:
            assert self.state is not None
            _, active, current, children = self.state
            updated = self.store.save_indexed_entry_metadata(
                self.record.id, entry, existing_record=self.record
            )
            projected = project_tree_entry(
                entry,
                leaf_id=entry.id if current else self.record.leaf_id,
                active=active,
                child_count=children,
            )
        else:
            updated = self.store.save_entry_metadata(
                self.record.id,
                entry,
                existing_record=self.record,
                tree_index=SessionTreeIndex(
                    self.record.conversation_entries, self.record.leaf_id
                ),
            )
            projected = next(
                item
                for item in project_tree(updated.conversation_entries, updated.leaf_id)
                if item.id == entry.id
            )
        return updated, projected
