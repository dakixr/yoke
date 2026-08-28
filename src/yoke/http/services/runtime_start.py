"""Fast persisted-state handoff for HTTP-owned runtime turns."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.agent.skills.models import ActiveSkill
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.session.admissions import INPUT_ID_METADATA_KEY
from yoke.session import SessionRecord
from yoke.session import SessionStore


@dataclass(slots=True)
class IndexedRuntimeStart:
    """Compaction-bounded runtime state read without full session parsing."""

    record: SessionRecord
    entries: list[ConversationEntry]
    persistence: RuntimeAppendPersistence


@dataclass(slots=True)
class RuntimeAppendPersistence:
    """Append only the new turn suffix back onto canonical persisted topology."""

    runtime_entry_count: int
    leaf_id: str

    def append(
        self,
        store: SessionStore,
        session_id: str,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: list[ActiveSkill] | None,
    ) -> SessionRecord:
        """Persist entries added after the compact runtime seed."""
        if len(entries) < self.runtime_entry_count:
            raise ValueError("Indexed runtime history shrank during a turn.")

        summary = store.summary_record(session_id)
        if summary is None:
            raise FileNotFoundError(session_id)
        if summary.leaf_id != self.leaf_id:
            raise ValueError("Persisted session leaf changed during indexed execution.")

        suffix = [
            entry.model_copy(deep=True) for entry in entries[self.runtime_entry_count :]
        ]
        if not suffix:
            if active_skills is None or active_skills == summary.active_skills:
                return summary
            return store.save_indexed_tree_navigation(
                session_id,
                existing_record=summary,
                leaf_id=self.leaf_id,
                active_skills=active_skills,
            )
        if suffix[0].parent_id != self.leaf_id:
            raise ValueError("Indexed runtime suffix does not extend the saved leaf.")
        for previous, current in zip(suffix, suffix[1:], strict=False):
            if current.parent_id != previous.id:
                raise ValueError("Indexed runtime suffix is not append-only.")
        if input_id is not None:
            _tag_input_entry(suffix, input_id)

        updated = store.save_indexed_tree_navigation(
            session_id,
            existing_record=summary,
            leaf_id=suffix[-1].id,
            appended_entries=tuple(suffix),
            active_skills=active_skills,
        )
        self.runtime_entry_count = len(entries)
        self.leaf_id = suffix[-1].id
        return updated


def indexed_runtime_start(
    store: SessionStore,
    message_index: SessionMessageIndex | None,
    session_id: str,
    *,
    has_attachments: bool,
) -> IndexedRuntimeStart | None:
    """Return an indexed runtime seed when its checkpoint is self-contained."""
    if message_index is None or has_attachments:
        return None
    seed = message_index.runtime_seed(session_id)
    if seed is None:
        return None
    record = store.summary_record(session_id)
    if record is None:
        return None
    if record.leaf_id is None or record.leaf_id != seed.leaf_id:
        return None
    return IndexedRuntimeStart(
        record=record,
        entries=seed.entries,
        persistence=RuntimeAppendPersistence(
            runtime_entry_count=len(seed.entries),
            leaf_id=record.leaf_id,
        ),
    )


def _tag_input_entry(entries: list[ConversationEntry], input_id: str) -> None:
    if any(entry.metadata.get(INPUT_ID_METADATA_KEY) == input_id for entry in entries):
        return
    for entry in reversed(entries):
        if entry.kind == "user" and entry.message is not None:
            entry.metadata[INPUT_ID_METADATA_KEY] = input_id
            return
