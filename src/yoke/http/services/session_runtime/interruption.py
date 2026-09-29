"""Persist logical interruption without creating a user row for continuation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.http.services.runtime_persistence import input_is_persisted
from yoke.http.services.runtime_persistence import tag_continuation_entries
from yoke.http.services.runtime_persistence import tag_input_entry
from yoke.http.services.runtime_persistence import with_turn_summary
from yoke.session.admissions import AdmissionRecord
from yoke.session.interrupt import interrupted_turn_snapshot

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime


def persist_interruption(
    runtime: SessionRuntime,
    admission: AdmissionRecord,
    *,
    duration_seconds: float | None = None,
    tool_count: int = 0,
) -> None:
    with runtime._persistence_lock:
        snapshot = runtime._snapshot()
        record = snapshot.record
        # Historical payloads are immutable. Only the recovery/checkpoint suffix
        # is new; copying a long tool-heavy conversation can exceed ACP's cancel
        # deadline while blocking the native runtime's event loop.
        active_entries = snapshot.active_path_entries
        user_message = (
            None
            if admission.continuation or input_is_persisted(record, admission.id)
            else runtime._user_message_for_admission(record, admission)
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
        runtime.store.save_indexed_tree_navigation(
            runtime.session_id,
            existing_record=record,
            leaf_id=suffix[-1].id,
            appended_entries=tuple(suffix),
            clear_context_usage=False,
        )
