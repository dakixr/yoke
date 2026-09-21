"""Persist logical interruption without creating a user row for continuation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.http.services.runtime_persistence import input_is_persisted
from yoke.http.services.runtime_persistence import tag_continuation_entries
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
        active_entries = snapshot.owned_active_path()
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
        if duration_seconds is not None:
            entries = with_turn_summary(
                entries, duration_seconds=duration_seconds, tool_count=tool_count
            )
        if admission.continuation:
            entries = tag_continuation_entries(
                entries, admission.id, {entry.id for entry in active_entries}
            )
        runtime._save_entries_locked(
            entries, input_id=None if admission.continuation else admission.id
        )
