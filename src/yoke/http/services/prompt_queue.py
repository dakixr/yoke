"""Pure admission identity, queue editing, and HTTP projections."""

from __future__ import annotations

import hashlib
import json

from yoke.http.errors import ApiError
from yoke.http.models.prompt import (
    PromptAdmissionReceipt,
    PromptAttachment,
    PromptInput,
    QueueData,
    QueueItem,
)
from yoke.session.admissions import AdmissionAttachment, AdmissionRecord
from yoke.session.queue import PersistedPendingInput, PersistedPromptQueue


def fingerprint(session_id: str, prompt: PromptInput, delivery: str) -> str:
    payload = prompt.model_dump(mode="json", by_alias=True)
    if not prompt.continuation:
        payload.pop("continuation")  # Preserve identities admitted before this field.
    raw = json.dumps(
        {
            "sessionID": session_id,
            "prompt": payload,
            "delivery": delivery,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def legacy_fingerprint(session_id: str, prompt: str, delivery: str) -> str:
    raw = json.dumps(
        {"sessionID": session_id, "prompt": prompt, "delivery": delivery},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def receipt(record: AdmissionRecord) -> PromptAdmissionReceipt:
    return PromptAdmissionReceipt(
        id=record.id,
        session_id=record.session_id,
        prompt=prompt_from_admission(record),
        delivery=record.delivery,
        state=record.state,
        admitted_seq=record.admitted_seq,
        promoted_seq=record.promoted_seq,
        time_created=record.time_created,
    )


def prompt_from_admission(record: AdmissionRecord) -> PromptInput:
    return PromptInput(
        text=record.prompt,
        continuation=record.continuation,
        attachments=[
            PromptAttachment(uri=item.uri, name=item.name, mime=item.mime)
            for item in record.attachments
        ],
    )


def queue_data(queue: PersistedPromptQueue) -> QueueData:
    return QueueData(
        revision=queue.revision,
        items=[
            QueueItem(
                id=item.id,
                prompt=PromptInput(
                    text=item.prompt,
                    continuation=item.continuation,
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


def apply_operation(items, admissions, operation) -> None:  # noqa: ANN001
    index = _item_index(items, operation.id)
    item = items[index]
    admission = admissions.get(item.id)
    if admission is None or admission.state != "admitted":
        raise ApiError(
            409, "queue_item_not_editable", "Queue item is no longer editable."
        )
    if operation.op == "update":
        item.prompt = operation.prompt.text
        item.continuation = operation.prompt.continuation
        admission.continuation = operation.prompt.continuation
        item.attachments = [
            {"uri": attachment.uri, "name": attachment.name, "mime": attachment.mime}
            for attachment in operation.prompt.attachments
        ]
        admission.prompt = operation.prompt.text
        admission.attachments = [
            AdmissionAttachment(
                uri=attachment.uri, name=attachment.name, mime=attachment.mime
            )
            for attachment in operation.prompt.attachments
        ]
        admission.fingerprint = fingerprint(
            admission.session_id, operation.prompt, admission.delivery
        )
        return
    if operation.op == "setDelivery":
        item.kind = "steering" if operation.delivery == "steer" else "queued"
        admission.delivery = operation.delivery
        admission.fingerprint = fingerprint(
            admission.session_id, prompt_from_admission(admission), admission.delivery
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


def next_eligible_index(
    items: list[PersistedPendingInput], *, allow_queue: bool
) -> int | None:
    for index, item in enumerate(items):
        if item.kind == "steering" and not item.paused:
            return index
    if allow_queue:
        for index, item in enumerate(items):
            if not item.paused:
                return index
    return None
