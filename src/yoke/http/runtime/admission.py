"""Reserve promoted input ownership before scheduling physical workers."""

from __future__ import annotations

from concurrent.futures import Executor
from contextlib import ExitStack

from yoke.http.runtime.entry_persistence import (
    input_has_terminal_assistant,
    input_is_persisted,
)
from yoke.http.runtime.persistence import TurnPersistence, run_blocking
from yoke.http.runtime.status import RuntimeStatus
from yoke.http.runtime.phase import Idle
from yoke.http.runtime.turns import ReservedInput
from yoke.http.runtime.workspace import require_runtime_workspace
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.queue import prompt_queue_transaction
from yoke.session.workspace import WorkspaceUnavailable, workspace_lease
from yoke.session.workspace_inputs import reconcile_workspace_inputs


class TurnAdmission:
    """Own admission reservation and recovery under a workspace lease."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        pending_inputs: PendingInputService,
        persistence: TurnPersistence,
        events: EventService,
        activity: RuntimeStatus,
        storage_executor: Executor,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.pending_inputs = pending_inputs
        self.persistence = persistence
        self.events = events
        self.activity = activity
        self.storage_executor = storage_executor

    def reserve(
        self, *, allow_queue: bool = True, recover: bool = True
    ) -> ReservedInput | None:
        """Cover promotion, waiting for capacity, execution, and cleanup with one lease.

        A promoted input without this lease is an orphan after a process exits.
        Acquiring before promotion makes that distinction safe across processes.
        """
        with ExitStack() as use:
            use.enter_context(workspace_lease(self.store, self.session_id))
            require_runtime_workspace(self)
            if recover:
                while True:
                    promoted = self.pending_inputs.unsettled_promoted(self.session_id)
                    if promoted is None:
                        break
                    snapshot = self.persistence.snapshot()
                    if not input_is_persisted(snapshot.record, promoted.id):
                        return ReservedInput(promoted, use.pop_all())
                    if not input_has_terminal_assistant(
                        snapshot.active_path_entries, promoted.id
                    ):
                        self.persistence.persist_interruption(promoted)
                    self.pending_inputs.settle(
                        self.session_id, promoted.id, outcome="recovered"
                    )
            admission = self.pending_inputs.pop_next(
                self.session_id, allow_queue=allow_queue
            )
            return (
                ReservedInput(admission, use.pop_all())
                if admission is not None
                else None
            )

    def pause(self, admission: AdmissionRecord) -> None:
        """Put an input back in the queue if its directory vanished after admission."""
        pending = self.pending_inputs
        with prompt_queue_transaction(
            self.store.directory, self.session_id
        ) as transaction:
            admissions = pending.admissions.load(self.session_id)
            current = admissions.records.get(admission.id)
            if current is None or current.settled or current.state == "removed":
                return
            current.workspace_blocked = True
            reconcile_workspace_inputs(
                pending.admissions, self.session_id, transaction, snapshot=admissions
            )
            queue = transaction.snapshot
            record = self.store.summary_record(self.session_id)
            self.events.durable(
                self.session_id,
                "session.queue.updated",
                {"revision": queue.revision, "reason": "workspace_unavailable"},
                location=record.root if record is not None else None,
            )

    async def reserve_input(
        self, *, allow_queue: bool = True, recover: bool = True
    ) -> ReservedInput | None:
        """Recover input on a worker and report workspace failures on the loop."""
        try:
            return await run_blocking(
                self.storage_executor,
                self.reserve,
                allow_queue=allow_queue,
                recover=recover,
            )
        except WorkspaceUnavailable as exc:
            if self.activity.active is None and self.activity.operation is None:
                self.activity.enter(
                    Idle(str(exc), "workspace_unavailable"),
                    cause="workspace_unavailable",
                )
            return None
