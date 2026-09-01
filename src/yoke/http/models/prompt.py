"""Prompt admission and queue-editor transport models."""

from __future__ import annotations

from typing import Annotated
from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel


class PromptAttachment(ApiModel):
    type: Literal["file"] = "file"
    uri: str
    name: str
    mime: str


class PromptInput(ApiModel):
    text: str = ""
    attachments: list[PromptAttachment] = Field(default_factory=list)


class PromptAdmissionRequest(ApiModel):
    id: str | None = None
    prompt: PromptInput
    delivery: Literal["steer", "queue"] = "steer"
    resume: bool = True


class PromptAdmissionReceipt(ApiModel):
    id: str
    session_id: str
    prompt: PromptInput
    delivery: Literal["steer", "queue"]
    state: Literal["admitted", "promoted", "removed"]
    admitted_seq: int
    promoted_seq: int | None = None
    time_created: str


class PromptAdmissionResponse(ApiModel):
    data: PromptAdmissionReceipt


class QueueItem(ApiModel):
    id: str
    prompt: PromptInput
    delivery: Literal["steer", "queue"]
    paused: bool
    created_at: str
    state: Literal["admitted"] = "admitted"


class QueueData(ApiModel):
    revision: int
    items: list[QueueItem]


class QueueResponse(ApiModel):
    data: QueueData


class QueueUpdateOperation(ApiModel):
    op: Literal["update"]
    id: str
    prompt: PromptInput


class QueueDeliveryOperation(ApiModel):
    op: Literal["setDelivery"]
    id: str
    delivery: Literal["steer", "queue"]


class QueuePausedOperation(ApiModel):
    op: Literal["setPaused"]
    id: str
    paused: bool


class QueueRemoveOperation(ApiModel):
    op: Literal["remove"]
    id: str


class QueueMoveBeforeOperation(ApiModel):
    op: Literal["moveBefore"]
    id: str
    before_id: str


class QueueMoveAfterOperation(ApiModel):
    op: Literal["moveAfter"]
    id: str
    after_id: str


class QueueMoveToStartOperation(ApiModel):
    op: Literal["moveToStart"]
    id: str


type QueueOperation = Annotated[
    QueueUpdateOperation
    | QueueDeliveryOperation
    | QueuePausedOperation
    | QueueRemoveOperation
    | QueueMoveBeforeOperation
    | QueueMoveAfterOperation
    | QueueMoveToStartOperation,
    Field(discriminator="op"),
]


class QueuePatchRequest(ApiModel):
    expected_revision: int = Field(ge=0)
    operations: list[QueueOperation]
