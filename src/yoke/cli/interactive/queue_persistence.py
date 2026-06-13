"""Persistent prompt queue helpers for the prompt-toolkit CLI."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from yoke.agent.models import Message
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.runtime import ActiveSession


class PersistedPendingPrompt(BaseModel):
    """One persisted queued or steering prompt."""

    id: str
    prompt: str
    kind: Literal["queued", "steering"] = "queued"
    created_at: str
    paused: bool = False
    user_message: Message | None = None


class PersistedPromptQueue(BaseModel):
    """Sidecar prompt queue payload."""

    version: int = 1
    prompts: list[PersistedPendingPrompt] = Field(default_factory=list)
    pending_images: list[str] = Field(default_factory=list)


def load_prompt_queue(
    active_session: ActiveSession,
) -> tuple[list[PendingPrompt], list[ImageAttachment]]:
    """Load the persisted prompt queue sidecar for a session."""
    path = prompt_queue_path(active_session)
    if not path.exists():
        return [], []
    try:
        payload = PersistedPromptQueue.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValidationError, ValueError):
        return [], []
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
    path = prompt_queue_path(active_session)
    active_prompts = [prompt for prompt in prompts if not prompt.paused]
    paused_prompts = [prompt for prompt in prompts if prompt.paused]
    ordered_prompts = active_prompts + paused_prompts
    images = pending_images or []
    if not ordered_prompts and not images:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    payload = PersistedPromptQueue(
        prompts=[
            PersistedPendingPrompt(
                id=prompt.id,
                prompt=prompt.prompt,
                kind=prompt.kind,
                created_at=prompt.created_at,
                paused=prompt.paused,
                user_message=prompt.user_message,
            )
            for prompt in ordered_prompts
        ],
        pending_images=[str(image.path) for image in images],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def clear_prompt_queue(active_session: ActiveSession) -> None:
    """Remove persisted queue state for a session."""
    try:
        prompt_queue_path(active_session).unlink()
    except FileNotFoundError:
        pass


def prompt_queue_path(active_session: ActiveSession) -> Path:
    """Return the sidecar path for a session prompt queue."""
    return active_session.store.directory / "queues" / f"{active_session.id}.json"


def now_iso() -> str:
    """Return a UTC timestamp for prompt queue entries."""
    return datetime.now(UTC).isoformat(timespec="seconds")
