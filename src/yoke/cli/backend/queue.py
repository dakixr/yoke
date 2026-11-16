"""Persistent prompt queue helpers for the prompt-toolkit CLI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yoke.cli.image_input import ImageAttachment
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.values import (
    LoadedPromptQueue as LoadedPromptQueue,
    PromptQueueRevisionConflict as PromptQueueRevisionConflict,
)

if TYPE_CHECKING:
    from yoke.cli.runtime import ActiveSession
from yoke.session.queue import (
    PersistedPendingInput,
    PersistedPromptQueue,
    load_prompt_queue_snapshot,
    prompt_queue_transaction,
)


def load_prompt_queue_state(active_session: ActiveSession) -> LoadedPromptQueue:
    """Load queue contents and their revision from one sidecar read."""
    remote = getattr(active_session, "backend", None)
    if remote is not None and hasattr(remote, "load_queue"):
        return remote.load_queue()
    payload = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    return _loaded_queue(payload)


def load_prompt_queue(
    active_session: ActiveSession,
) -> tuple[list[PendingPrompt], list[ImageAttachment]]:
    """Load the persisted prompt queue sidecar for a session."""
    loaded = load_prompt_queue_state(active_session)
    return loaded.prompts, loaded.pending_images


def _loaded_queue(payload: PersistedPromptQueue) -> LoadedPromptQueue:
    """Project one persisted queue into CLI values."""
    prompts = [
        PendingPrompt(
            prompt=item.prompt,
            kind=item.kind,
            user_message=item.user_message,
            id=item.id,
            created_at=item.created_at,
            paused=item.paused,
            attachments=[dict(attachment) for attachment in item.attachments],
            continuation=item.continuation,
        )
        for item in payload.prompts
    ]
    images = [
        ImageAttachment(path=Path(raw_path))
        for raw_path in payload.pending_images
        if raw_path
    ]
    return LoadedPromptQueue(prompts, images, payload.revision)


def persist_prompt_queue(
    active_session: ActiveSession,
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
    active_session: ActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
    *,
    expected_revision: int | None = None,
) -> int:
    """Persist a CLI queue snapshot and return its committed revision."""
    remote = getattr(active_session, "backend", None)
    if remote is not None and hasattr(remote, "commit_queue"):
        return remote.commit_queue(
            prompts,
            pending_images or [],
            expected_revision=expected_revision,
        )
    images = pending_images if pending_images is not None else []
    with prompt_queue_transaction(
        active_session.store.directory,
        active_session.id,
    ) as transaction:
        existing = transaction.snapshot
        _check_revision(existing, expected_revision)
        existing_by_id = {item.id: item for item in existing.prompts}
        payload = PersistedPromptQueue(
            revision=existing.revision,
            prompts=[
                PersistedPendingInput(
                    id=prompt.id,
                    prompt=prompt.prompt,
                    continuation=prompt.continuation,
                    attachments=(
                        list(existing_by_id[prompt.id].attachments)
                        if prompt.id in existing_by_id
                        else [dict(attachment) for attachment in prompt.attachments]
                    ),
                    kind=prompt.kind,
                    created_at=prompt.created_at,
                    paused=prompt.paused,
                    user_message=prompt.user_message,
                )
                for prompt in prompts
            ],
            pending_images=[str(image.path) for image in images],
        )
        if payload.model_dump(exclude={"revision"}) == existing.model_dump(
            exclude={"revision"}
        ):
            return existing.revision
        payload.revision = existing.revision + 1
        transaction.snapshot = payload
        transaction.commit()
        return payload.revision


def clear_prompt_queue(
    active_session: ActiveSession,
    *,
    expected_revision: int | None = None,
) -> None:
    """Clear queued work while keeping the shared revision monotonic."""
    remote = getattr(active_session, "backend", None)
    if remote is not None and hasattr(remote, "clear_queue"):
        remote.clear_queue(expected_revision=expected_revision)
        return
    with prompt_queue_transaction(
        active_session.store.directory,
        active_session.id,
    ) as transaction:
        existing = transaction.snapshot
        _check_revision(existing, expected_revision)
        if (
            existing.revision == 0
            and not existing.prompts
            and not existing.pending_images
        ):
            return
        transaction.snapshot = PersistedPromptQueue(revision=existing.revision + 1)
        transaction.commit()


def _check_revision(
    existing: PersistedPromptQueue,
    expected_revision: int | None,
) -> None:
    """Reject a stale replacement while the queue transaction is held."""
    if expected_revision is None or existing.revision == expected_revision:
        return
    raise PromptQueueRevisionConflict(
        expected_revision=expected_revision,
        actual_revision=existing.revision,
    )
