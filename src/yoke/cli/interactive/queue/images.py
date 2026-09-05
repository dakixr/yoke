"""Revision-aware pending-image actions for the prompt-toolkit CLI."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.persistence import LoadedPromptQueue
from yoke.cli.interactive.queue.persistence import PromptQueueRevisionConflict
from yoke.cli.interactive.queue.persistence import commit_prompt_queue
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.runtime import ActiveSession


def attach_pending_image(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    attachment: ImageAttachment,
) -> None:
    """Persist one image append without replacing stale prompt values."""
    with state_lock:
        _ensure_session(state, active_session)
        while True:
            images = [*state.pending_images, attachment]
            if _persist_images(state, active_session, images):
                state.pending_images = images
                return
            _reload(state, active_session)


def remove_pending_image(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    index: int = -1,
) -> ImageAttachment | None:
    """Persist removal of the selected image while preserving queue items."""
    with state_lock:
        _ensure_session(state, active_session)
        _refresh_if_changed(state, active_session)
        if not state.pending_images:
            return None
        normalized_index = index if index >= 0 else len(state.pending_images) + index
        if normalized_index < 0 or normalized_index >= len(state.pending_images):
            return None
        target = state.pending_images[normalized_index]
        occurrence = sum(
            image.path == target.path
            for image in state.pending_images[: normalized_index + 1]
        )
        while True:
            remote_index = _path_occurrence_index(
                state.pending_images,
                target.path,
                occurrence,
            )
            if remote_index is None:
                return None
            images = [
                image
                for candidate_index, image in enumerate(state.pending_images)
                if candidate_index != remote_index
            ]
            if _persist_images(state, active_session, images):
                state.pending_images = images
                return target
            _reload(state, active_session)


def consume_pending_images(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    images: list[ImageAttachment] | None = None,
) -> list[ImageAttachment]:
    """Persist consumption of one submission's stable image snapshot."""
    with state_lock:
        _ensure_session(state, active_session)
        _refresh_if_changed(state, active_session)
        consumed = list(state.pending_images if images is None else images)
        if not consumed:
            return []
        consumed_paths = [image.path for image in consumed]
        while True:
            remaining = _remove_paths(state.pending_images, consumed_paths)
            if _persist_images(state, active_session, remaining):
                state.pending_images = remaining
                return consumed
            _reload(state, active_session)


def _persist_images(
    state: PromptCliState,
    active_session: ActiveSession,
    images: list[ImageAttachment],
) -> bool:
    try:
        state.queue_revision = commit_prompt_queue(
            active_session,
            state.pending_prompts,
            images,
            expected_revision=state.queue_revision,
        )
    except PromptQueueRevisionConflict:
        return False
    return True


def _ensure_session(state: PromptCliState, active_session: ActiveSession) -> None:
    if state.queue_session_id is None:
        state.queue_session_id = active_session.id
    elif state.queue_session_id != active_session.id:
        _reload(state, active_session)


def _refresh_if_changed(
    state: PromptCliState,
    active_session: ActiveSession,
) -> None:
    loaded = load_prompt_queue_state(active_session)
    if loaded.revision != state.queue_revision:
        _install_loaded(state, active_session, loaded)


def _reload(state: PromptCliState, active_session: ActiveSession) -> None:
    _install_loaded(state, active_session, load_prompt_queue_state(active_session))


def _install_loaded(
    state: PromptCliState,
    active_session: ActiveSession,
    loaded: LoadedPromptQueue,
) -> None:
    state.pending_prompts = loaded.prompts
    state.pending_images = loaded.pending_images
    state.queue_revision = loaded.revision
    state.queue_session_id = active_session.id


def _path_occurrence_index(
    images: list[ImageAttachment],
    path: Path,
    occurrence: int,
) -> int | None:
    seen = 0
    for index, image in enumerate(images):
        if image.path != path:
            continue
        seen += 1
        if seen == occurrence:
            return index
    return None


def _remove_paths(
    images: list[ImageAttachment],
    paths: list[Path],
) -> list[ImageAttachment]:
    remaining = list(images)
    for path in paths:
        index = next(
            (index for index, image in enumerate(remaining) if image.path == path),
            None,
        )
        if index is not None:
            remaining.pop(index)
    return remaining
