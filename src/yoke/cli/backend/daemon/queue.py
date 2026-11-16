"""Revision-checked queue editing, without a second terminal execution queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.backend.daemon import DaemonActiveSession

from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.daemon.images import upload_message_images
from pathlib import Path
from yoke.cli.backend.values import LoadedPromptQueue, PromptQueueRevisionConflict
from yoke.cli.image_input import ImageAttachment
from yoke.http.models.prompt import (
    PromptAttachment,
    PromptAdmissionRequest,
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
        return LoadedPromptQueue(
            prompts,
            [ImageAttachment(Path(path)) for path in data.pending_images],
            data.revision,
        )

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
        same_prompts = len(prompts) == len(loaded.prompts) and all(
            (
                left.id,
                left.prompt,
                left.kind,
                left.paused,
                left.continuation,
                left.attachments,
            )
            == (
                right.id,
                right.prompt,
                right.kind,
                right.paused,
                right.continuation,
                right.attachments,
            )
            and (left.user_message is None or not left.user_message.has_image_inputs())
            for left, right in zip(prompts, loaded.prompts, strict=True)
        )
        if same_prompts and pending_images == loaded.pending_images:
            return expected
        admissions: list[PromptAdmissionRequest] = []
        existing = {item.id: item for item in loaded.prompts}
        desired = {item.id for item in prompts}
        operations: list[QueueOperation] = [
            QueueRemoveOperation(op="remove", id=item.id)
            for item in loaded.prompts
            if item.id not in desired
        ]
        for index, item in enumerate(prompts):
            from yoke.cli.interactive.skill_commands import parse_skill_command

            command = parse_skill_command(item.prompt)
            attachments = item.attachments
            if not attachments and item.user_message is not None:
                attachments = upload_message_images(self.connection, item.user_message)
            payload = PromptInput(
                text=item.prompt,
                skill=command[0] if command is not None and not command[1] else None,
                attachments=[
                    PromptAttachment.model_validate(value) for value in attachments
                ],
                continuation=item.continuation,
            )
            delivery = "steer" if item.kind == "steering" else "queue"
            if item.id not in existing:
                admissions.append(
                    PromptAdmissionRequest(
                        id=item.id,
                        client_id=self.connection.owner,
                        delivery=delivery,
                        resume=False,
                        prompt=payload,
                    )
                )
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
        if operations or pending_images != loaded.pending_images:
            try:
                data = QueueResponse.model_validate(
                    self.connection.session_request(
                        "PATCH",
                        "/queue",
                        json=QueuePatchRequest(
                            expected_revision=expected,
                            operations=operations,
                            admissions=admissions,
                            pending_images=[
                                str(image.path) for image in pending_images
                            ],
                            resume=False,
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
        return expected

    def clear(self, *, expected_revision: int | None) -> None:
        self.commit([], [], expected_revision=expected_revision)


def load_prompt_queue_state(active_session: DaemonActiveSession) -> LoadedPromptQueue:
    return active_session.backend.load_queue()


def load_prompt_queue(
    active_session: DaemonActiveSession,
) -> tuple[list[PendingPrompt], list[ImageAttachment]]:
    """Read queued prompts and image drafts from the daemon."""
    loaded = load_prompt_queue_state(active_session)
    return loaded.prompts, loaded.pending_images


def persist_prompt_queue(
    active_session: DaemonActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
    *,
    expected_revision: int | None = None,
) -> None:
    """Persist queued prompts and pending attachments for crash-safe resume."""
    commit_prompt_queue(
        active_session,
        prompts,
        pending_images,
        expected_revision=expected_revision,
    )


def commit_prompt_queue(
    active_session: DaemonActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
    *,
    expected_revision: int | None = None,
) -> int:
    return active_session.backend.commit_queue(
        prompts, pending_images or [], expected_revision=expected_revision
    )


def clear_prompt_queue(
    active_session: DaemonActiveSession,
    *,
    expected_revision: int | None = None,
) -> None:
    active_session.backend.clear_queue(expected_revision=expected_revision)
