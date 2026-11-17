"""Crash-recoverable transfer of workspace-blocked inputs back to the queue."""

from __future__ import annotations

from yoke.session.admissions import AdmissionSnapshot, AdmissionStore
from yoke.session.queue import PromptQueueTransaction
from yoke.session.input_lifecycle import InputEvents


def reconcile_workspace_inputs(
    admissions: AdmissionStore,
    session_id: str,
    transaction: PromptQueueTransaction,
    *,
    snapshot: AdmissionSnapshot | None = None,
    orphaned: bool = False,
    events: InputEvents | None = None,
) -> bool:
    """Materialize durable pause intent without losing promoted recovery state.

    Call under the queue transaction. ``orphaned`` additionally requires an
    exclusive workspace lease, proving that no CLI or HTTP execution still owns
    a promoted input. Every write ordering is recoverable: intent, queue, then
    admission state. Repeating after any interrupted write is idempotent.
    """
    snapshot = snapshot if snapshot is not None else admissions.load(session_id)
    queue = transaction.snapshot
    queued = {item.id: item for item in queue.prompts}
    repair = [
        item
        for item in snapshot.records.values()
        if not item.settled
        and item.state != "removed"
        and (item.workspace_blocked or (orphaned and item.state == "promoted"))
    ]
    if not repair:
        return False
    from yoke.session.input_lifecycle import InputLifecycle
    from yoke.session.input_phase import InputEvent, Paused, PauseCause

    lifecycle = InputLifecycle(admissions, events)
    changed = False
    for item in sorted(repair, key=lambda value: value.admitted_seq, reverse=True):
        existing = queued.get(item.id)
        if existing is not None and existing.paused and isinstance(item.phase, Paused):
            continue
        position = item.queue_position if item.queue_position is not None else 0
        lifecycle.apply(
            transaction,
            session_id,
            item,
            InputEvent.BLOCK_WORKSPACE,
            Paused(position, PauseCause.WORKSPACE),
        )
        changed = True
    if changed:
        queue.held_reason = "workspace_unavailable"
        queue.revision += 1
        transaction.commit()
        lifecycle.reindex(transaction, session_id)
    return changed
