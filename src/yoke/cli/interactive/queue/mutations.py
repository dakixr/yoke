"""Serialized queue mutations for the prompt-toolkit CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.images import (
    attach_pending_image as attach_pending_image,
)
from yoke.cli.interactive.queue.images import (
    consume_pending_images as consume_pending_images,
)
from yoke.cli.interactive.queue.images import (
    remove_pending_image as remove_pending_image,
)
from yoke.cli.interactive.queue.persistence import LoadedPromptQueue
from yoke.cli.interactive.queue.persistence import PromptQueueRevisionConflict
from yoke.cli.interactive.queue.persistence import commit_prompt_queue
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.runtime import ActiveSession

QUEUE_MANAGER_CONFLICT_NOTICE = (
    "Queue changed while the manager was open. Reloaded the latest queue; "
    "your manager changes were not saved."
)


@dataclass(frozen=True, slots=True)
class DequeuedPrompt:
    """A durably removed prompt and its authoritative list position."""

    prompt: PendingPrompt
    index: int


def rebind_prompt_queue(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
) -> None:
    """Replace local queue state with one session's persisted queue."""
    with state_lock:
        _reload(state, active_session)


def replace_prompt_queue(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    prompts: list[PendingPrompt],
    expected_revision: int,
) -> bool:
    """Save a manager replacement, rejecting any concurrent queue change."""
    with state_lock:
        _ensure_session(state, active_session)
        if state.queue_revision != expected_revision:
            _reload(state, active_session)
            return False
        replacement = [prompt.copy_for_queue() for prompt in prompts]
        try:
            revision = commit_prompt_queue(
                active_session,
                replacement,
                state.pending_images,
                expected_revision=expected_revision,
            )
        except PromptQueueRevisionConflict:
            _reload(state, active_session)
            return False
        state.pending_prompts = replacement
        state.queue_revision = revision
        return True


def append_prompt(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    prompt: PendingPrompt,
) -> bool:
    """Append one new item, rebasing only that append after a conflict."""
    return queue_prompt_submission(
        state=state,
        state_lock=state_lock,
        active_session=active_session,
        prompt=prompt,
        consumed_images=[],
    )


def queue_prompt_submission(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    prompt: PendingPrompt,
    consumed_images: list[ImageAttachment],
) -> bool:
    """Commit one submitted prompt and its consumed images together."""
    with state_lock:
        _ensure_session(state, active_session)
        consumed_paths = [image.path for image in consumed_images]
        while True:
            matching = next(
                (item for item in state.pending_prompts if item.id == prompt.id),
                None,
            )
            images = _remove_paths(state.pending_images, consumed_paths)
            if matching is not None:
                if matching == prompt:
                    try:
                        state.queue_revision = commit_prompt_queue(
                            active_session,
                            state.pending_prompts,
                            images,
                            expected_revision=state.queue_revision,
                        )
                    except PromptQueueRevisionConflict:
                        _reload(state, active_session)
                        continue
                    state.pending_images = images
                    return True
                loaded = load_prompt_queue_state(active_session)
                if loaded.revision != state.queue_revision:
                    _install_loaded(state, active_session, loaded)
                    continue
                state.status_message = (
                    f"Could not queue prompt: item ID {prompt.id} already exists."
                )
                return False
            replacement = [*state.pending_prompts, prompt.copy_for_queue()]
            try:
                revision = commit_prompt_queue(
                    active_session,
                    replacement,
                    images,
                    expected_revision=state.queue_revision,
                )
            except PromptQueueRevisionConflict:
                _reload(state, active_session)
                continue
            state.pending_prompts = replacement
            state.pending_images = images
            state.queue_revision = revision
            return True


def observe_prompt_submission(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
) -> tuple[list[ImageAttachment], bool]:
    """Return stable images and idleness after observing the current revision."""
    with state_lock:
        _ensure_session(state, active_session)
        _refresh_if_changed(state, active_session)
        idle = state.worker is None and not state.pending_prompts
        return list(state.pending_images), idle


def dequeue_prompt(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    steering_only: bool = False,
) -> PendingPrompt | None:
    """Remove and return the next eligible item from authoritative state."""
    dequeued = dequeue_prompt_with_position(
        state=state,
        state_lock=state_lock,
        active_session=active_session,
        steering_only=steering_only,
    )
    return None if dequeued is None else dequeued.prompt


def dequeue_prompt_with_position(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    steering_only: bool = False,
) -> DequeuedPrompt | None:
    """Remove the next authoritative item and retain its position for recovery."""
    with state_lock:
        _ensure_session(state, active_session)
        while True:
            index = next_pending_prompt_index(
                state.pending_prompts,
                steering_only=steering_only,
            )
            if index is None:
                loaded = load_prompt_queue_state(active_session)
                if loaded.revision != state.queue_revision:
                    _install_loaded(state, active_session, loaded)
                    continue
                return None
            selected = state.pending_prompts[index]
            replacement = [
                prompt
                for candidate_index, prompt in enumerate(state.pending_prompts)
                if candidate_index != index
            ]
            try:
                revision = commit_prompt_queue(
                    active_session,
                    replacement,
                    state.pending_images,
                    expected_revision=state.queue_revision,
                )
            except PromptQueueRevisionConflict:
                _reload(state, active_session)
                continue
            state.pending_prompts = replacement
            state.queue_revision = revision
            return DequeuedPrompt(selected, index)


def restore_dequeued_prompt(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    dequeued: DequeuedPrompt,
) -> None:
    """Restore an unaccepted dequeued item at its authoritative position."""
    with state_lock:
        _ensure_session(state, active_session)
        while True:
            matching = next(
                (
                    item
                    for item in state.pending_prompts
                    if item.id == dequeued.prompt.id
                ),
                None,
            )
            if matching is not None:
                if matching != dequeued.prompt:
                    raise RuntimeError(
                        f"Could not restore queue item {dequeued.prompt.id}: "
                        "the ID is already in use."
                    )
                return
            replacement = list(state.pending_prompts)
            replacement.insert(
                min(dequeued.index, len(replacement)),
                dequeued.prompt.copy_for_queue(),
            )
            try:
                revision = commit_prompt_queue(
                    active_session,
                    replacement,
                    state.pending_images,
                    expected_revision=state.queue_revision,
                )
            except PromptQueueRevisionConflict:
                _reload(state, active_session)
                continue
            state.pending_prompts = replacement
            state.queue_revision = revision
            return


def _ensure_session(state: PromptCliState, active_session: ActiveSession) -> None:
    if state.queue_session_id is None:
        state.queue_session_id = active_session.id
    elif state.queue_session_id != active_session.id:
        _reload(state, active_session)


def _reload(state: PromptCliState, active_session: ActiveSession) -> None:
    _install_loaded(state, active_session, load_prompt_queue_state(active_session))


def _refresh_if_changed(
    state: PromptCliState,
    active_session: ActiveSession,
) -> None:
    loaded = load_prompt_queue_state(active_session)
    if loaded.revision != state.queue_revision:
        _install_loaded(state, active_session, loaded)


def _install_loaded(
    state: PromptCliState,
    active_session: ActiveSession,
    loaded: LoadedPromptQueue,
) -> None:
    state.pending_prompts = loaded.prompts
    state.pending_images = loaded.pending_images
    state.queue_revision = loaded.revision
    state.queue_session_id = active_session.id


def next_pending_prompt_index(
    prompts: list[PendingPrompt],
    *,
    steering_only: bool = False,
) -> int | None:
    """Return the first runnable steering item, then ordinary queued work."""
    for index, prompt in enumerate(prompts):
        if prompt.kind == "steering" and not prompt.paused:
            return index
    if steering_only:
        return None
    for index, prompt in enumerate(prompts):
        if not prompt.paused:
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
