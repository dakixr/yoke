"""Durable prompt admission and atomic pending-queue edits."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
import secrets
from typing import Literal

from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptAdmissionReceipt
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.models.prompt import PromptInput
from yoke.http.models.prompt import QueueData
from yoke.http.models.prompt import QueuePatchRequest
from yoke.http.services.event_broker import EventService
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.workspace import (
    WorkspaceBusy,
    inspect_workspace,
    require_session_workspace,
    workspace_lease,
)
from yoke.session.workspace_inputs import reconcile_workspace_inputs
from yoke.session.admissions import AdmissionAttachment
from yoke.session.admissions import AdmissionRecord
from yoke.session.admissions import AdmissionStore
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import prompt_queue_transaction
from yoke.http.services.prompt_queue import (
    apply_operation as _apply_operation,
    fingerprint as _fingerprint,
    legacy_fingerprint as _legacy_fingerprint,
    next_eligible_index as _next_eligible_index,
    prompt_from_admission as _prompt_from_admission,
    queue_data as _queue_data,
    receipt as _receipt,
)


class PendingInputService:
    """Serialize prompt admission and queue mutations for each session."""

    def __init__(
        self,
        store: SessionStore,
        admissions: AdmissionStore,
        events: EventService,
        uploads: UploadService,
    ) -> None:
        self.store = store
        self.admissions = admissions
        self.events = events
        self.uploads = uploads

    def admit(
        self,
        session_id: str,
        request: PromptAdmissionRequest,
    ) -> PromptAdmissionReceipt:
        """Durably admit one idempotent input before any model-visible promotion."""
        record = self._require_session(session_id)
        self._validate_prompt(session_id, request.prompt)
        input_id = request.id or f"inp_{secrets.token_hex(12)}"
        fingerprint = _fingerprint(session_id, request.prompt, request.delivery)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            snapshot = self.admissions.load(session_id)
            reconcile_workspace_inputs(
                self.admissions, session_id, transaction, snapshot=snapshot
            )
            existing = snapshot.records.get(input_id)
            if existing is not None:
                legacy_match = (
                    not existing.attachments
                    and not request.prompt.attachments
                    and existing.fingerprint
                    == _legacy_fingerprint(
                        session_id,
                        request.prompt.text,
                        request.delivery,
                    )
                )
                if existing.fingerprint != fingerprint and not legacy_match:
                    raise ApiError(
                        409,
                        "input_identity_conflict",
                        "Input id was already used with different admission data.",
                        {"inputID": input_id},
                    )
                return _receipt(existing)

            # Relocation uses this queue lock too. Load the binding inside it,
            # after identity replay but before unarchiving, pinning or admission.
            record = self._require_session(session_id)
            require_session_workspace(record)
            if record.archived_at is not None:
                record = self.store.set_archived(
                    session_id,
                    False,
                    existing_record=record,
                )
                self.events.durable(
                    session_id,
                    "session.updated",
                    {
                        "sessionID": record.id,
                        "title": record.title,
                        "pinned": record.pinned,
                        "archivedAt": record.archived_at,
                    },
                    location=record.root,
                )

            self._pin_prompt(session_id, request.prompt)

            event = self.events.durable(
                session_id,
                "session.prompt.admitted",
                {
                    "inputID": input_id,
                    "delivery": request.delivery,
                    "prompt": request.prompt.model_dump(mode="json", by_alias=True),
                },
                location=record.root,
            )
            admitted_seq = event.durable.seq if event.durable is not None else 0
            admission = AdmissionRecord(
                id=input_id,
                session_id=session_id,
                prompt=request.prompt.text,
                attachments=[
                    AdmissionAttachment(
                        uri=item.uri,
                        name=item.name,
                        mime=item.mime,
                    )
                    for item in request.prompt.attachments
                ],
                delivery=request.delivery,
                fingerprint=fingerprint,
                time_created=datetime.now(UTC).isoformat(),
                admitted_seq=admitted_seq,
            )
            queue = transaction.snapshot
            queue.prompts.append(
                PersistedPendingInput(
                    id=input_id,
                    prompt=request.prompt.text,
                    attachments=[
                        {
                            "uri": item.uri,
                            "name": item.name,
                            "mime": item.mime,
                        }
                        for item in request.prompt.attachments
                    ],
                    kind="steering" if request.delivery == "steer" else "queued",
                    created_at=admission.time_created,
                )
            )
            queue.revision += 1
            snapshot.records[input_id] = admission
            self.admissions.save(session_id, snapshot)
            transaction.commit()
            self.events.durable(
                session_id,
                "session.queue.updated",
                {"revision": queue.revision},
                location=record.root,
            )
            return _receipt(admission)

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
                self.admissions, session_id, transaction, orphaned=orphaned
            )
            return _queue_data(transaction.snapshot)

    def patch_queue(self, session_id: str, request: QueuePatchRequest) -> QueueData:
        record = self._require_session(session_id)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            reconcile_workspace_inputs(self.admissions, session_id, transaction)
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
            for operation in request.operations:
                if operation.op == "update":
                    self._validate_prompt(session_id, operation.prompt)
                    edited_ids.add(operation.id)
                elif operation.op == "setDelivery":
                    edited_ids.add(operation.id)
                elif operation.op == "remove":
                    removed_ids.add(operation.id)
                _apply_operation(prompts, admissions.records, operation)
            for prompt in prompts:
                for attachment in prompt.attachments:
                    self.uploads.pin(
                        attachment["uri"],
                        session_id=session_id,
                        name=attachment["name"],
                        mime=attachment["mime"],
                    )
            queue.prompts = prompts
            queue.revision += 1
            self.admissions.save(session_id, admissions)
            transaction.commit()
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
            reconcile_workspace_inputs(self.admissions, session_id, transaction)
            queue = transaction.snapshot
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
            admission.state = "promoted"
            admission.promoted_seq = (
                promoted.durable.seq if promoted.durable is not None else None
            )
            queue.revision += 1
            admissions.records[admission.id] = admission
            self.admissions.save(session_id, admissions)
            transaction.commit()
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
            reconcile_workspace_inputs(self.admissions, session_id, transaction)
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
        with prompt_queue_transaction(self.store.directory, session_id):
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
            admission.settled = True
            admission.settled_at = datetime.now(UTC).isoformat()
            admission.outcome = outcome
            admissions.records[input_id] = admission
            self.admissions.save(session_id, admissions)
            self.events.durable(
                session_id,
                "session.prompt.settled",
                {"inputID": input_id, "outcome": outcome},
                location=record.root,
            )

    def _require_session(self, session_id: str):  # noqa: ANN202
        record = self.store.summary_record(session_id)
        if record is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        return record

    def _validate_prompt(self, session_id: str, prompt: PromptInput) -> None:
        if len(prompt.attachments) > 20:
            raise ApiError(
                400,
                "too_many_attachments",
                "Prompt accepts at most 20 attachments.",
            )
        for attachment in prompt.attachments:
            self.uploads.validate_reference(
                attachment.uri,
                session_id=session_id,
                name=attachment.name,
                mime=attachment.mime,
            )
        if not prompt.text.strip() and not prompt.attachments:
            raise ApiError(400, "empty_prompt", "Prompt text cannot be empty.")
        if len(prompt.text.encode()) > 1_048_576:
            raise ApiError(413, "prompt_too_large", "Prompt exceeds the server limit.")

    def _pin_prompt(self, session_id: str, prompt: PromptInput) -> None:
        for attachment in prompt.attachments:
            self.uploads.pin(
                attachment.uri,
                session_id=session_id,
                name=attachment.name,
                mime=attachment.mime,
            )
