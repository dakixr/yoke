"""Prompt submission helpers for the prompt-toolkit interactive loop."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from threading import Thread

from yoke.cli.image_input import build_user_message
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.runtime import ActiveSession
from yoke.cli.interactive.queue.persistence import persist_prompt_queue


def submit_prompt_toolkit_prompt(
    prompt: str,
    *,
    action: str,
    state: PromptCliState,
    active_session: ActiveSession,
    state_lock: Lock,
    invalidate_prompt: Callable[[], None],
    start_turn: Callable[..., Thread],
    steer_active_turn: Callable[..., bool],
) -> None:
    """Submit a normal prompt without reprocessing slash commands."""
    with state_lock:
        idle = state.worker is None and not state.pending_prompts
        pending_images = [image.path for image in state.pending_images]
        user_message = build_user_message(prompt, image_paths=pending_images)
        state.pending_images.clear()
        if not idle and action == "queue":
            state.pending_prompts.append(
                PendingPrompt(
                    prompt,
                    user_message=user_message,
                    kind="queued",
                )
            )
            persist_prompt_queue(
                active_session, state.pending_prompts, state.pending_images
            )
    if idle:
        start_turn(prompt, user_message=user_message)
        return
    if action == "queue":
        invalidate_prompt()
        return
    if steer_active_turn(prompt, user_message=user_message):
        return
    with state_lock:
        state.pending_prompts.append(
            PendingPrompt(
                prompt,
                user_message=user_message,
                kind="queued",
            )
        )
        persist_prompt_queue(
            active_session, state.pending_prompts, state.pending_images
        )
    invalidate_prompt()
