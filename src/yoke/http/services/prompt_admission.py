"""Durable prompt admission and atomic pending-queue edits."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.prompt import (
    PromptAdmissionReceipt,
    PromptAdmissionRequest,
    PromptInput,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.prompt_queue import (
    fingerprint as _fingerprint,
    legacy_fingerprint as _legacy_fingerprint,
    receipt as _receipt,
)
from yoke.http.runtime.prompt_input import validate_prompt
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionAttachment, AdmissionRecord, AdmissionStore
from yoke.session.queue import prompt_queue_transaction
from yoke.session.input_lifecycle import InputLifecycle
from yoke.session.input_phase import (
    InputEvent,
    Queued,
)
from yoke.session.workspace import (
    require_session_workspace,
)
from yoke.session.workspace_inputs import reconcile_workspace_inputs


class PromptAdmissionService:
    """Own identity replay, validation and atomic admission before runtime wake."""

    def __init__(
        self,
        store: SessionStore,
        admissions: AdmissionStore,
        events: EventService,
        uploads: UploadService,
    ) -> None:
        self.store, self.admissions, self.events, self.uploads = (
            store,
            admissions,
            events,
            uploads,
        )
        self.lifecycle = InputLifecycle(admissions, events)

    def admit(
        self,
        session_id: str,
        request: PromptAdmissionRequest,
        *,
        publish_queue: bool = True,
    ) -> PromptAdmissionReceipt:
        """Durably admit one idempotent input before any model-visible promotion."""
        record = self._require_session(session_id)
        self._validate_prompt(session_id, request.prompt)
        input_id = request.id or f"inp_{secrets.token_hex(12)}"
        fingerprint = _fingerprint(session_id, request.prompt, request.delivery)
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            snapshot = self.admissions.load(session_id)
            reconcile_workspace_inputs(
                self.admissions,
                session_id,
                transaction,
                snapshot=snapshot,
                events=self.events,
            )
            if request.resume:
                transaction.snapshot.held_reason = None
                transaction.commit()
            existing = snapshot.records.get(input_id)
            if existing is not None:
                legacy_match = (
                    not existing.attachments
                    and not existing.continuation
                    and not request.prompt.continuation
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
                    "clientID": request.client_id,
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
                continuation=request.prompt.continuation,
                skill=request.prompt.skill,
                attachments=[
                    AdmissionAttachment(
                        uri=item.uri,
                        name=item.name,
                        mime=item.mime,
                        image_projection=item.image_projection,
                    )
                    for item in request.prompt.attachments
                ],
                delivery=request.delivery,
                fingerprint=fingerprint,
                time_created=datetime.now(UTC).isoformat(),
                admitted_seq=admitted_seq,
                client_id=request.client_id,
            )
            queue = transaction.snapshot
            if publish_queue:
                queue.revision += 1
            self.lifecycle.apply(
                transaction,
                session_id,
                admission,
                InputEvent.ADMIT,
                Queued(len(queue.prompts)),
            )
            if publish_queue:
                self.events.durable(
                    session_id,
                    "session.queue.updated",
                    {"revision": queue.revision},
                    location=record.root,
                )
            return _receipt(admission)

    def _require_session(self, session_id: str):  # noqa: ANN202
        record = self.store.summary_record(session_id)
        if record is None:
            raise session_not_found()
        return record

    def _validate_prompt(self, session_id: str, prompt: PromptInput) -> None:
        validate_prompt(self, session_id, prompt)

    def _pin_prompt(self, session_id: str, prompt: PromptInput) -> None:
        for attachment in prompt.attachments:
            self.uploads.pin(
                attachment.uri,
                session_id=session_id,
                name=attachment.name,
                mime=attachment.mime,
            )
