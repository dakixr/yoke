"""Durable prompt admission and atomic pending-queue edits."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.prompt import (
    PromptAdmissionReceipt,
    PromptAdmissionRequest,
    PromptInput,
    QueueData,
    QueuePatchRequest,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.prompt_queue import (
    apply_operation as _apply_operation,
    next_eligible_index as _next_eligible_index,
    prompt_from_admission as _prompt_from_admission,
    queue_data as _queue_data,
)
from yoke.http.runtime.prompt_input import validate_prompt
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionRecord, AdmissionStore
from yoke.session.queue import prompt_queue_transaction
from yoke.session.input_lifecycle import InputLifecycle
from yoke.session.input_phase import (
    InputEvent,
    Queued,
    Paused,
    Promoted,
    Removed,
    Settled,
    PauseCause,
)
from yoke.http.services.input_service import InputService
from yoke.session.workspace import (
    WorkspaceBusy,
    inspect_workspace,
    workspace_lease,
)
from yoke.session.workspace_inputs import reconcile_workspace_inputs


class PendingInputService:
    """Serialize prompt admission and queue mutations for each session."""

    def __init__(
        self,
        store: SessionStore,
        admissions: AdmissionStore,
        events: EventService,
        uploads: UploadService,
    ) -> None:
        from yoke.http.services.prompt_admission import PromptAdmissionService

        self.admit = PromptAdmissionService(store, admissions, events, uploads).admit
        self.store = store
        self.admissions = admissions
        self.events = events
        self.uploads = uploads
        self.lifecycle = InputLifecycle(admissions, events)
        self.control = InputService(store, admissions, events, self.lifecycle)
        self.hold_stranded = self.control.hold_stranded
        self.set_hold = self.control.set_hold
        self.get_input = self.control.get_input
        self.receipt = self.control.receipt
        self.start_input = self.control.start
        self.recover = self.control.recover

    def queue(self, session_id: str) -> QueueData:
        record = self._require_session(session_id)
        if not inspect_workspace(record.root).available:
            try:
                with workspace_lease(self.store, session_id, exclusive=True):
                    return self._queue_snapshot(session_id, orphaned=True)
            except WorkspaceBusy:
                pass  # A live worker may still own its promoted input.
        return self._queue_snapshot(session_id)

    def _queue_snapshot(self, session_id: str, *, orphaned: bool = False) -> QueueData:
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            reconcile_workspace_inputs(
                self.admissions,
                session_id,
                transaction,
                orphaned=orphaned,
                events=self.events,
            )
            return _queue_data(transaction.snapshot)

    def patch_queue(
        self,
        session_id: str,
        request: QueuePatchRequest,
        *,
        admit: Callable[[PromptAdmissionRequest], PromptAdmissionReceipt] | None = None,
    ) -> QueueData:
        record = self._require_session(session_id)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            reconcile_workspace_inputs(
                self.admissions, session_id, transaction, events=self.events
            )
            queue = transaction.snapshot
            if queue.revision != request.expected_revision:
                raise ApiError(
                    409,
                    "queue_revision_conflict",
                    f"Queue changed since revision {request.expected_revision}.",
                    {
                        "expectedRevision": request.expected_revision,
                        "actualRevision": queue.revision,
                    },
                )
            for admission in request.admissions:
                if admit is None:
                    self.admit(session_id, admission, publish_queue=False)
                else:
                    admit(admission)
            if request.admissions:
                with prompt_queue_transaction(
                    self.store.directory, session_id
                ) as fresh:
                    queue = fresh.snapshot
                    transaction.snapshot = queue
            admissions = self.admissions.load(session_id)
            prompts = [item.model_copy(deep=True) for item in queue.prompts]
            prior_attachments = {
                attachment.uri: (attachment.name, attachment.mime)
                for admission in admissions.records.values()
                if admission.state == "admitted"
                for attachment in admission.attachments
            }
            edited_ids: set[str] = set()
            removed_ids: set[str] = set()
            if request.resume:
                queue.held_reason = None
            for operation in request.operations:
                if operation.op == "update":
                    self._validate_prompt(session_id, operation.prompt)
                    edited_ids.add(operation.id)
                elif operation.op == "setDelivery":
                    edited_ids.add(operation.id)
                elif operation.op == "remove":
                    removed_ids.add(operation.id)
                _apply_operation(prompts, admissions.records, operation)
                current = admissions.records[operation.id]
                if operation.op == "remove":
                    target, event = Removed(), InputEvent.REMOVE
                else:
                    position = next(
                        i for i, item in enumerate(prompts) if item.id == operation.id
                    )
                    if operation.op == "setPaused":
                        target = (
                            Paused(position, PauseCause.USER)
                            if operation.paused
                            else Queued(position)
                        )
                        event = (
                            InputEvent.PAUSE if operation.paused else InputEvent.RESUME
                        )
                        if isinstance(current.phase, type(target)):
                            target = current.phase
                            event = InputEvent.EDIT
                    else:
                        target = (
                            Paused(position, current.phase.cause)
                            if isinstance(current.phase, Paused)
                            else Queued(position)
                        )
                        event = (
                            InputEvent.MOVE
                            if operation.op.startswith("move")
                            else InputEvent.EDIT
                        )
                self.lifecycle.apply(transaction, session_id, current, event, target)
                prompts = list(queue.prompts)
                admissions = self.admissions.load(session_id)
            for prompt in prompts:
                for attachment in prompt.attachments:
                    self.uploads.pin(
                        attachment["uri"],
                        session_id=session_id,
                        name=attachment["name"],
                        mime=attachment["mime"],
                    )
            queue.prompts = prompts
            if request.pending_images is not None:
                queue.pending_images = list(request.pending_images)
            queue.revision += 1
            transaction.commit()
            self.lifecycle.reindex(transaction, session_id)
            retained_uris = {
                attachment.uri
                for admission in admissions.records.values()
                if admission.state != "removed"
                for attachment in admission.attachments
            }
            for uri in prior_attachments.keys() - retained_uris:
                self.uploads.delete_bound_upload(uri, session_id=session_id)
            for input_id in sorted(edited_ids - removed_ids):
                admission = admissions.records.get(input_id)
                if admission is None:
                    continue
                self.events.durable(
                    session_id,
                    "session.prompt.edited",
                    {
                        "inputID": input_id,
                        "delivery": admission.delivery,
                        "prompt": _prompt_from_admission(admission).model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                    location=record.root,
                )
            for input_id in sorted(removed_ids):
                self.events.durable(
                    session_id,
                    "session.prompt.removed",
                    {"inputID": input_id},
                    location=record.root,
                )
            self.events.durable(
                session_id,
                "session.queue.updated",
                {
                    "revision": queue.revision,
                    "operations": len(request.operations),
                },
                location=record.root,
            )
            return _queue_data(queue)

    def pop_next(self, session_id: str, *, allow_queue: bool) -> AdmissionRecord | None:
        """Atomically promote the next eligible input for one runtime drain."""
        record = self._require_session(session_id)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            reconcile_workspace_inputs(
                self.admissions, session_id, transaction, events=self.events
            )
            queue = transaction.snapshot
            if queue.held_reason is not None:
                return None
            index = _next_eligible_index(queue.prompts, allow_queue=allow_queue)
            if index is None:
                return None
            pending = queue.prompts.pop(index)
            admissions = self.admissions.load(session_id)
            admission = admissions.records.get(pending.id)
            if admission is None or admission.state != "admitted":
                queue.revision += 1
                transaction.commit()
                return None
            promoted = self.events.durable(
                session_id,
                "session.prompt.promoted",
                {"inputID": admission.id, "delivery": admission.delivery},
                location=record.root,
            )
            seq = promoted.durable.seq if promoted.durable is not None else 0
            queue.revision += 1
            self.lifecycle.apply(
                transaction,
                session_id,
                admission,
                InputEvent.PROMOTE,
                Promoted(seq, index),
                replacing=not allow_queue,
            )
            self.lifecycle.reindex(transaction, session_id)
            self.events.durable(
                session_id,
                "session.queue.updated",
                {"revision": queue.revision},
                location=record.root,
            )
            return admission.model_copy(deep=True)

    def unsettled_promoted(self, session_id: str) -> AdmissionRecord | None:
        """Return the oldest promoted input that has no terminal runtime outcome."""
        self._require_session(session_id)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            reconcile_workspace_inputs(
                self.admissions, session_id, transaction, events=self.events
            )
            admissions = self.admissions.load(session_id)
            promoted = [
                item
                for item in admissions.records.values()
                if item.state == "promoted" and not item.settled
            ]
            if not promoted:
                return None
            promoted.sort(
                key=lambda item: (
                    item.promoted_seq if item.promoted_seq is not None else 2**63,
                    item.admitted_seq,
                )
            )
            return promoted[0].model_copy(deep=True)

    def settle(
        self,
        session_id: str,
        input_id: str,
        *,
        outcome: Literal["completed", "stopped", "failed", "recovered"],
    ) -> None:
        """Mark one promoted input as having reached a terminal runtime boundary."""
        record = self._require_session(session_id)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            admissions = self.admissions.load(session_id)
            admission = admissions.records.get(input_id)
            if admission is None:
                raise ApiError(404, "input_not_found", "Input was not found.")
            if admission.state != "promoted":
                raise ApiError(
                    409,
                    "input_not_promoted",
                    "Only promoted inputs can be settled.",
                )
            if admission.settled:
                return
            self.lifecycle.apply(
                transaction,
                session_id,
                admission,
                InputEvent.RECOVER_SETTLE
                if outcome == "recovered"
                else InputEvent.SETTLE,
                Settled(outcome, datetime.now(UTC).isoformat()),
            )
            self.events.durable(
                session_id,
                "session.prompt.settled",
                {"inputID": input_id, "outcome": outcome},
                location=record.root,
            )

    def _require_session(self, session_id: str):  # noqa: ANN202
        record = self.store.summary_record(session_id)
        if record is None:
            raise session_not_found()
        return record

    def _validate_prompt(self, session_id: str, prompt: PromptInput) -> None:
        validate_prompt(self, session_id, prompt)
