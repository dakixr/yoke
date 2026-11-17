"""Shared interactive CLI models and helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from typing import Protocol

from yoke.agent.models import ConversationEntry, Message
from yoke.cli.backend.session_view import SessionView
from yoke.cli.image_input import ImageAttachment
from yoke.cli.backend.models import (
    PendingPrompt as PendingPrompt,
    TurnFailure as TurnFailure,
    TurnStopped as TurnStopped,
    TurnSuccess as TurnSuccess,
)
from yoke.cli.backend.daemon import DaemonSessionBackend

COMPACTION_IN_PROGRESS_NOTICE = "Compacting conversation..."


def session_resume_notice(session_id: str) -> str:
    """Return the canonical command shown when leaving an interactive session."""
    return f"To resume this session run:\nyoke resume {session_id}"


class InputFunc(Protocol):
    """Input function protocol."""

    def __call__(self, prompt: object = "", /) -> str:
        """Read the next input value."""
        ...


@dataclass(slots=True)
class InputInterrupted:
    """Sentinel for keyboard interruption during input."""


@dataclass(slots=True)
class BasicCliState:
    """Mutable state for the basic interactive CLI loop."""

    messages: list[Message]
    pending_prompts: list[PendingPrompt]
    view: SessionView | None = None
    pending_images: list[ImageAttachment] = field(default_factory=list)
    worker: Thread | None = None
    active_stop_request: Event | None = None
    shutdown_requested: bool = False
    rendering: dict[str, Callable[[], None]] = field(default_factory=dict, repr=False)
    pending_results: int = 0
    wakeup: Event = field(default_factory=Event, repr=False)
    input_closed: bool = False
    exit_notice_emitted: bool = False


@dataclass(slots=True)
class PromptCliState:
    """Mutable state for the prompt-toolkit interactive loop."""

    messages: list[Message]
    pending_prompts: list[PendingPrompt]
    view: SessionView | None = None
    pending_images: list[ImageAttachment] = field(default_factory=list)
    queue_lock: RLock = field(default_factory=RLock, repr=False)
    queue_revision: int = 0
    queue_session_id: str | None = None
    worker: Thread | None = None
    turn_handoff_active: bool = False
    active_stop_request: Event | None = None
    active_user_message: Message | None = None
    # Rendering identities remain compatible with existing render integrations.
    # Daemon input IDs own modern display fences; neither determines busy.
    active_turn_id: int = 0
    rendering_inputs: dict[int, str] = field(default_factory=dict)
    fenced_inputs: set[str] = field(default_factory=set)
    abandoned_turn_ids: set[int] = field(default_factory=set)
    steered_turn_ids: set[int] = field(default_factory=set)
    shutdown_requested: bool = False
    exit_notice_emitted: bool = False
    status_message: str = ""
    submit_action: str = "steer"
    editor_revision: int = 0
    context_usage_text: str | None = None
    context_usage_revision: int = 0
    context_usage_percent: int | None = None
    context_input_tokens: int | None = None
    context_max_tokens: int | None = None
    turn_start_time: float | None = None
    turn_tool_count: int = 0
    turn_input_tokens: int | None = None
    turn_output_tokens: int | None = None
    turn_reasoning_tokens: int | None = None
    spinner_index: int = 0
    thinking_effort: str | None = None
    next_editor_text: str | None = None


def session_busy(state: PromptCliState | BasicCliState) -> bool:
    """Read daemon activity; legacy callers without a view retain local rendering."""
    return state.view.busy if state.view is not None else state.worker is not None


def format_pending_summary(
    pending_prompts: Sequence[str | PendingPrompt],
) -> str:
    """Format prompt queue summary for the prompt toolbar."""
    queued_count = 0
    steering_count = 0
    for prompt in pending_prompts:
        if isinstance(prompt, PendingPrompt) and prompt.kind == "steering":
            steering_count += 1
        else:
            queued_count += 1
    parts: list[str] = []
    if steering_count:
        parts.append(f"{steering_count} steering")
    if queued_count:
        parts.append(f"{queued_count} queued")
    return f" · {' · '.join(parts)}" if parts else ""


def format_context_usage_text(
    usage: Mapping[str, object] | None,
) -> str | None:
    """Format estimated remaining context capacity for the toolbar."""
    if usage is None:
        return None
    usage_percent = usage.get("usage_percent")
    if not isinstance(usage_percent, int):
        return None
    left_percent = min(100, max(0, 100 - usage_percent))
    return f"{left_percent}% left"


def parse_context_usage_details(
    usage: Mapping[str, object] | None,
) -> dict[str, int | None]:
    """Extract context usage details from a usage payload."""
    if usage is None:
        return {"usage_percent": None, "input_tokens": None, "max_tokens": None}
    usage_percent = usage.get("usage_percent")
    input_tokens = usage.get("input_tokens")
    max_tokens = usage.get("max_total_tokens")
    return {
        "usage_percent": (usage_percent if isinstance(usage_percent, int) else None),
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "max_tokens": max_tokens if isinstance(max_tokens, int) else None,
    }


def estimate_context_usage_text(
    agent: DaemonSessionBackend,
    prompt: str,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> str | None:
    """Estimate remaining context budget as toolbar text."""
    usage = agent.context_usage(
        prompt,
        messages,
        conversation_entries=conversation_entries,
    )
    return format_context_usage_text(usage)
