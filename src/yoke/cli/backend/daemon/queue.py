"""Revision-checked queue editing, without a second terminal execution queue."""

from __future__ import annotations

from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.values import LoadedPromptQueue, PromptQueueRevisionConflict
from yoke.cli.image_input import ImageAttachment
from yoke.http.models.prompt import (
    PromptAttachment,
    PromptInput,
    QueueDeliveryOperation,
    QueueOperation,
    QueueMoveAfterOperation,
    QueueMoveToStartOperation,
    QueuePatchRequest,
    QueuePausedOperation,
    QueueRemoveOperation,
    QueueResponse,
    QueueUpdateOperation,
)


class DaemonPromptQueue:
    """Translate queue snapshots and serialize edits against their revision.

    New admissions stay dormant until the final patch has installed pause and
    delivery settings. Only the daemon promotes inputs, including CLI inputs.
    """

    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection
        self._pending_images: list[ImageAttachment] = []

    def load(self) -> LoadedPromptQueue:
        data = QueueResponse.model_validate(
            self.connection.session_request("GET", "/queue")
        ).data
        prompts = [
            PendingPrompt(
                prompt=item.prompt.text,
                kind="steering" if item.delivery == "steer" else "queued",
                id=item.id,
                created_at=item.created_at,
                paused=item.paused,
                attachments=[
                    {
                        key: value
                        for key, value in attachment.model_dump(by_alias=True).items()
                        if isinstance(value, str)
                    }
                    for attachment in item.prompt.attachments
                ],
                continuation=item.prompt.continuation,
            )
            for item in data.items
        ]
        return LoadedPromptQueue(prompts, list(self._pending_images), data.revision)

    def commit(
        self,
        prompts: list[PendingPrompt],
        pending_images: list[ImageAttachment],
        *,
        expected_revision: int | None,
    ) -> int:
        loaded = self.load()
        expected = loaded.revision if expected_revision is None else expected_revision
        if expected != loaded.revision:
            raise PromptQueueRevisionConflict(
                expected_revision=expected, actual_revision=loaded.revision
            )
        existing = {item.id: item for item in loaded.prompts}
        desired = {item.id for item in prompts}
        operations: list[QueueOperation] = [
            QueueRemoveOperation(op="remove", id=item.id)
            for item in loaded.prompts
            if item.id not in desired
        ]
        for index, item in enumerate(prompts):
            payload = PromptInput(
                text=item.prompt,
                attachments=[
                    PromptAttachment.model_validate(value) for value in item.attachments
                ],
                continuation=item.continuation,
            )
            delivery = "steer" if item.kind == "steering" else "queue"
            if item.id not in existing:
                self.connection.session_request(
                    "POST",
                    "/prompt",
                    json={
                        "id": item.id,
                        "clientID": self.connection.owner,
                        "delivery": delivery,
                        "resume": False,
                        "prompt": payload.model_dump(by_alias=True),
                    },
                )
                expected += 1
            operations.append(
                QueueMoveToStartOperation(op="moveToStart", id=item.id)
                if index == 0
                else QueueMoveAfterOperation(
                    op="moveAfter", id=item.id, after_id=prompts[index - 1].id
                )
            )
            operations.extend(
                [
                    QueueUpdateOperation(op="update", id=item.id, prompt=payload),
                    QueueDeliveryOperation(
                        op="setDelivery", id=item.id, delivery=delivery
                    ),
                    QueuePausedOperation(
                        op="setPaused", id=item.id, paused=item.paused
                    ),
                ]
            )
        if operations:
            try:
                data = QueueResponse.model_validate(
                    self.connection.session_request(
                        "PATCH",
                        "/queue",
                        json=QueuePatchRequest(
                            expected_revision=expected, operations=operations
                        ).model_dump(by_alias=True),
                    )
                ).data
            except DaemonRequestError as exc:
                if exc.status != 409:
                    raise
                actual = self.load().revision
                raise PromptQueueRevisionConflict(
                    expected_revision=expected, actual_revision=actual
                ) from exc
            expected = data.revision
        self._pending_images = list(pending_images)
        return expected

    def clear(self, *, expected_revision: int | None) -> None:
        self.commit([], [], expected_revision=expected_revision)
