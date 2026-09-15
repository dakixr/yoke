"""Crash-recoverable transfer of workspace-blocked inputs back to the queue."""

from __future__ import annotations

from yoke.session.admissions import AdmissionSnapshot, AdmissionStore
from yoke.session.queue import PersistedPendingInput, PromptQueueTransaction


def reconcile_workspace_inputs(
    admissions: AdmissionStore,
    session_id: str,
    transaction: PromptQueueTransaction,
    *,
    snapshot: AdmissionSnapshot | None = None,
    orphaned: bool = False,
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
    for item in repair:
        item.workspace_blocked = True
    # Never remove the recoverable promoted state before replacement is durable.
    admissions.save(session_id, snapshot)
    changed = False
    for item in sorted(repair, key=lambda value: value.admitted_seq, reverse=True):
        existing = queued.get(item.id)
        if existing is None:
            restored = PersistedPendingInput(
                id=item.id,
                prompt=item.prompt,
                attachments=[
                    attachment.model_dump() for attachment in item.attachments
                ],
                kind="steering" if item.delivery == "steer" else "queued",
                created_at=item.time_created,
                paused=True,
            )
            queue.prompts.insert(0, restored)
            queued[item.id] = restored
            changed = True
        elif not existing.paused:
            existing.paused = True
            changed = True
    if changed:
        queue.revision += 1
        transaction.commit()
    for item in repair:
        item.state = "admitted"
        item.workspace_blocked = False
    admissions.save(session_id, snapshot)
    return changed
