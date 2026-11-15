"""Serialized checkpoint, turn, and interruption persistence."""

from __future__ import annotations

from threading import Lock

from yoke.agent.models import ConversationEntry, Message
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.runtime_persistence import (
    active_skill_list,
    input_is_persisted,
    tag_continuation_entries,
    tag_input_entry,
    with_turn_summary,
)
from yoke.http.services.session_read_cache import SessionReadCache, SessionReadSnapshot
from yoke.http.services.session_runtime.execution import (
    TurnExecution,
)
from yoke.http.services.session_runtime.prompt_input import (
    user_message as admission_user_message,
)
from yoke.session import SessionRecord, SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.interrupt import interrupted_turn_snapshot


class TurnPersistence:
    """Serialize checkpoints, final turns, and interruption on the same branch."""

    def __init__(
        self,
        session_id: str,
        store: SessionStore,
        pending_inputs: PendingInputService,
        read_cache: SessionReadCache,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.pending_inputs = pending_inputs
        self.read_cache = read_cache
        self.lock = Lock()

    def record(self) -> SessionRecord:
        """Return the shared signature-consistent record snapshot for this session."""
        return self.snapshot().record

    def record_for_execution(self, execution: TurnExecution) -> SessionRecord:
        """Return turn metadata without forcing a full parse for indexed starts."""
        if execution.append_persistence is None:
            return self.record()
        record = self.store.summary_record(self.session_id)
        if record is None:
            raise FileNotFoundError(self.session_id)
        return record

    def snapshot(self) -> SessionReadSnapshot:
        """Return the shared parsed snapshot and keep event location current."""
        snapshot = self.read_cache.get(self.session_id)
        return snapshot

    def persist_entries(
        self,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        with self.lock:
            self._save_entries_locked(
                entries,
                input_id=input_id,
                active_skills=active_skills,
            )

    def persist_turn_entries(
        self,
        execution: TurnExecution,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        with self.lock:
            # Interrupt persistence owns the saved branch once retirement wins.
            # Recheck under the same lock used by both writers so a worker that
            # passed an earlier checkpoint guard cannot overwrite that branch.
            if execution.retired_event.is_set():
                return
            if execution.admission.continuation:
                entries = tag_continuation_entries(
                    entries, execution.admission.id, execution.baseline_entry_ids
                )
                input_id = None
            persistence = execution.append_persistence
            if persistence is None:
                self._save_entries_locked(
                    entries,
                    input_id=input_id,
                    active_skills=active_skills,
                )
                return
            persistence.append(
                self.store,
                self.session_id,
                entries,
                input_id=input_id,
                active_skills=active_skill_list(active_skills),
            )

    def _save_entries_locked(
        self,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        current = self.record()
        copied = [entry.model_copy(deep=True) for entry in entries]
        if input_id is not None:
            tag_input_entry(copied, input_id)
        leaf_id = copied[-1].id if copied else current.leaf_id
        resolved_skills = active_skill_list(active_skills)
        skills = current.active_skills if resolved_skills is None else resolved_skills
        self.store.save(
            self.session_id,
            [],
            conversation_entries=copied,
            leaf_id=leaf_id,
            active_skills=skills,
            skill_dirs=current.skill_dirs,
            root=current.root,
            title=current.title,
            provider_name=current.provider_name,
            model_id=current.model_id,
            reasoning_effort=current.reasoning_effort,
            context_window_tokens=current.context_window_tokens,
            existing_record=current,
        )

    def user_message(
        self,
        record: SessionRecord,
        admission: AdmissionRecord,
    ) -> Message:
        return admission_user_message(
            self.pending_inputs, self.session_id, record, admission
        )

    def persist_interruption(
        self,
        admission: AdmissionRecord,
        *,
        duration_seconds: float | None = None,
        tool_count: int = 0,
    ) -> None:
        with self.lock:
            snapshot = self.snapshot()
            record = snapshot.record
            # Historical payloads are immutable. Only the recovery/checkpoint suffix
            # is new; copying a long tool-heavy conversation can exceed ACP's cancel
            # deadline while blocking the native runtime's event loop.
            active_entries = snapshot.active_path_entries
            user_message = (
                None
                if admission.continuation or input_is_persisted(record, admission.id)
                else self.user_message(record, admission)
            )
            _, entries = interrupted_turn_snapshot(
                messages=(),
                entries=active_entries,
                user_message=user_message,
                leaf_id=record.leaf_id,
            )
            suffix = entries[len(active_entries) :]
            if duration_seconds is not None:
                suffix = with_turn_summary(
                    suffix, duration_seconds=duration_seconds, tool_count=tool_count
                )
            if admission.continuation:
                suffix = tag_continuation_entries(suffix, admission.id, set())
            elif user_message is not None:
                tag_input_entry(suffix, admission.id)
            self.store.save_indexed_tree_navigation(
                self.session_id,
                existing_record=record,
                leaf_id=suffix[-1].id,
                appended_entries=tuple(suffix),
                clear_context_usage=False,
            )
