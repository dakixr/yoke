"""Durable prompt admission and atomic pending-queue edits."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
import hashlib
import json
import secrets
from threading import Lock
from typing import Literal

from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptAdmissionReceipt
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.models.prompt import PromptAttachment
from yoke.http.models.prompt import PromptInput
from yoke.http.models.prompt import QueueData
from yoke.http.models.prompt import QueueItem
from yoke.http.models.prompt import QueuePatchRequest
from yoke.http.services.event_broker import EventService
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionAttachment
from yoke.session.admissions import AdmissionRecord
from yoke.session.admissions import AdmissionStore
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.queue import write_prompt_queue_snapshot


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
        self._locks_lock = Lock()
        self._locks: dict[str, Lock] = {}

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
        with self._lock_for(session_id):
            snapshot = self.admissions.load(session_id)
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
            queue = load_prompt_queue_snapshot(self.store.directory, session_id)
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
            write_prompt_queue_snapshot(self.store.directory, session_id, queue)
            self.events.durable(
                session_id,
                "session.queue.updated",
                {"revision": queue.revision},
                location=record.root,
            )
            return _receipt(admission)

    def queue(self, session_id: str) -> QueueData:
        self._require_session(session_id)
        with self._lock_for(session_id):
            return _queue_data(
                load_prompt_queue_snapshot(self.store.directory, session_id)
            )

    def patch_queue(self, session_id: str, request: QueuePatchRequest) -> QueueData:
        record = self._require_session(session_id)
        with self._lock_for(session_id):
            queue = load_prompt_queue_snapshot(self.store.directory, session_id)
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
            write_prompt_queue_snapshot(self.store.directory, session_id, queue)
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
        with self._lock_for(session_id):
            queue = load_prompt_queue_snapshot(self.store.directory, session_id)
            index = _next_eligible_index(queue.prompts, allow_queue=allow_queue)
            if index is None:
                return None
            pending = queue.prompts.pop(index)
            admissions = self.admissions.load(session_id)
            admission = admissions.records.get(pending.id)
            if admission is None or admission.state != "admitted":
                queue.revision += 1
                write_prompt_queue_snapshot(self.store.directory, session_id, queue)
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
            write_prompt_queue_snapshot(self.store.directory, session_id, queue)
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
        with self._lock_for(session_id):
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
        with self._lock_for(session_id):
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

    def _lock_for(self, session_id: str) -> Lock:
        with self._locks_lock:
            return self._locks.setdefault(session_id, Lock())

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


def _fingerprint(session_id: str, prompt: PromptInput, delivery: str) -> str:
    raw = json.dumps(
        {
            "sessionID": session_id,
            "prompt": prompt.model_dump(mode="json", by_alias=True),
            "delivery": delivery,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _legacy_fingerprint(session_id: str, prompt: str, delivery: str) -> str:
    raw = json.dumps(
        {"sessionID": session_id, "prompt": prompt, "delivery": delivery},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _receipt(record: AdmissionRecord) -> PromptAdmissionReceipt:
    return PromptAdmissionReceipt(
        id=record.id,
        session_id=record.session_id,
        prompt=_prompt_from_admission(record),
        delivery=record.delivery,
        state=record.state,
        admitted_seq=record.admitted_seq,
        promoted_seq=record.promoted_seq,
        time_created=record.time_created,
    )


def _prompt_from_admission(record: AdmissionRecord) -> PromptInput:
    return PromptInput(
        text=record.prompt,
        attachments=[
            PromptAttachment(uri=item.uri, name=item.name, mime=item.mime)
            for item in record.attachments
        ],
    )


def _queue_data(queue: PersistedPromptQueue) -> QueueData:
    return QueueData(
        revision=queue.revision,
        items=[
            QueueItem(
                id=item.id,
                prompt=PromptInput(
                    text=item.prompt,
                    attachments=[
                        PromptAttachment.model_validate(value)
                        for value in item.attachments
                    ],
                ),
                delivery="steer" if item.kind == "steering" else "queue",
                paused=item.paused,
                created_at=item.created_at,
            )
            for item in queue.prompts
        ],
    )


def _item_index(items: list[PersistedPendingInput], input_id: str) -> int:
    for index, item in enumerate(items):
        if item.id == input_id:
            return index
    raise ApiError(404, "queue_item_not_found", "Queue item was not found.")


def _apply_operation(items, admissions, operation) -> None:  # noqa: ANN001
    index = _item_index(items, operation.id)
    item = items[index]
    admission = admissions.get(item.id)
    if admission is None or admission.state != "admitted":
        raise ApiError(
            409, "queue_item_not_editable", "Queue item is no longer editable."
        )
    if operation.op == "update":
        item.prompt = operation.prompt.text
        item.attachments = [
            {
                "uri": attachment.uri,
                "name": attachment.name,
                "mime": attachment.mime,
            }
            for attachment in operation.prompt.attachments
        ]
        admission.prompt = operation.prompt.text
        admission.attachments = [
            AdmissionAttachment(
                uri=attachment.uri,
                name=attachment.name,
                mime=attachment.mime,
            )
            for attachment in operation.prompt.attachments
        ]
        admission.fingerprint = _fingerprint(
            admission.session_id,
            operation.prompt,
            admission.delivery,
        )
        return
    if operation.op == "setDelivery":
        item.kind = "steering" if operation.delivery == "steer" else "queued"
        admission.delivery = operation.delivery
        admission.fingerprint = _fingerprint(
            admission.session_id,
            PromptInput(
                text=admission.prompt,
                attachments=[
                    PromptAttachment(uri=item.uri, name=item.name, mime=item.mime)
                    for item in admission.attachments
                ],
            ),
            admission.delivery,
        )
        return
    if operation.op == "setPaused":
        item.paused = operation.paused
        return
    if operation.op == "remove":
        items.pop(index)
        admission.state = "removed"
        return
    if operation.op == "moveToStart":
        items.pop(index)
        items.insert(0, item)
        return
    if operation.op == "moveBefore":
        target = _item_index(items, operation.before_id)
        items.pop(index)
        if index < target:
            target -= 1
        items.insert(target, item)
        return
    if operation.op == "moveAfter":
        target = _item_index(items, operation.after_id)
        items.pop(index)
        if index < target:
            target -= 1
        items.insert(target + 1, item)
        return
    raise ApiError(400, "invalid_queue_operation", "Unsupported queue operation.")


def _next_eligible_index(
    items: list[PersistedPendingInput],
    *,
    allow_queue: bool,
) -> int | None:
    for index, item in enumerate(items):
        if item.kind == "steering" and not item.paused:
            return index
    if allow_queue:
        for index, item in enumerate(items):
            if not item.paused:
                return index
    return None
