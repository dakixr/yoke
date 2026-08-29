"""Persistent prompt queue helpers for the prompt-toolkit CLI."""

from __future__ import annotations

from pathlib import Path
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.runtime import ActiveSession
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.queue import prompt_queue_path as shared_prompt_queue_path
from yoke.session.queue import write_prompt_queue_snapshot


def load_prompt_queue(
    active_session: ActiveSession,
) -> tuple[list[PendingPrompt], list[ImageAttachment]]:
    """Load the persisted prompt queue sidecar for a session."""
    payload = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    prompts = [
        PendingPrompt(
            prompt=item.prompt,
            kind=item.kind,
            user_message=item.user_message,
            id=item.id,
            created_at=item.created_at,
            paused=item.paused,
        )
        for item in payload.prompts
    ]
    images = [
        ImageAttachment(path=Path(raw_path))
        for raw_path in payload.pending_images
        if raw_path
    ]
    return prompts, images


def persist_prompt_queue(
    active_session: ActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
) -> None:
    """Persist queued prompts and pending attachments for crash-safe resume."""
    active_prompts = [prompt for prompt in prompts if not prompt.paused]
    paused_prompts = [prompt for prompt in prompts if prompt.paused]
    ordered_prompts = active_prompts + paused_prompts
    images = pending_images or []
    existing = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    existing_by_id = {item.id: item for item in existing.prompts}
    payload = PersistedPromptQueue(
        revision=existing.revision,
        prompts=[
            PersistedPendingInput(
                id=prompt.id,
                prompt=prompt.prompt,
                attachments=(
                    list(existing_by_id[prompt.id].attachments)
                    if prompt.id in existing_by_id
                    else []
                ),
                kind=prompt.kind,
                created_at=prompt.created_at,
                paused=prompt.paused,
                user_message=prompt.user_message,
            )
            for prompt in ordered_prompts
        ],
        pending_images=[str(image.path) for image in images],
    )
    if payload.model_dump(exclude={"revision"}) == existing.model_dump(
        exclude={"revision"}
    ):
        return
    payload.revision = existing.revision + 1
    write_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
        payload,
    )


def clear_prompt_queue(active_session: ActiveSession) -> None:
    """Clear queued work while keeping the shared revision monotonic."""
    existing = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    if existing.revision == 0 and not existing.prompts and not existing.pending_images:
        return
    write_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
        PersistedPromptQueue(revision=existing.revision + 1),
    )


def prompt_queue_path(active_session: ActiveSession) -> Path:
    """Return the sidecar path for a session prompt queue."""
    return shared_prompt_queue_path(active_session.store.directory, active_session.id)
