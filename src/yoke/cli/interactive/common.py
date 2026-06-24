"""Shared interactive CLI models and helpers."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from threading import Event
from threading import Thread
from typing import Literal
from typing import Protocol
from uuid import uuid4

from yoke.agent.context.manager import _drop_incomplete_tool_turns
from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.cli.image_input import ImageAttachment
from yoke.agent.models import ConversationEntry
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import estimate_context_usage

COMPACTION_IN_PROGRESS_NOTICE = "Compacting conversation..."
SHORTCUT_LINES = (
    "Type `exit` or `quit` to leave.",
    "Press `Enter` to send, or to steer during a turn.",
    "Press `Tab` to queue behind the current turn.",
    "Press `Shift+Tab` to cycle thinking effort.",
    "Press `Esc` twice to stop the current turn.",
    "Press `Ctrl+J` or `Shift+Enter` to insert a newline.",
    "Press `Esc` then `Enter` to insert a newline.",
    "Press `Ctrl+V` to paste text or attach an image.",
    "Press `Ctrl+U` to remove the last pending image.",
    "Press `Ctrl+O` to open the tool inspector.",
    "Press `Ctrl+Q` to open the queue manager.",
    "Press `Ctrl+X` then `M` to switch model.",
    "Press `Ctrl+X` then `T` to open the session tree.",
    "Use `/shortcuts` or `?` to show this list again.",
)
SHORTCUTS_NOTICE = "Keyboard shortcuts:\n" + "\n".join(SHORTCUT_LINES)


class InputFunc(Protocol):
    """Input function protocol."""

    def __call__(self, prompt: object = "", /) -> str:
        """Read the next input value."""
        ...


def handle_slash_command(*args, **kwargs):
    """Dispatch a slash command without importing the dispatcher eagerly."""
    from yoke.cli.interactive.slash_commands import handle_slash_command as dispatch

    return dispatch(*args, **kwargs)


@dataclass(slots=True)
class TurnSuccess:
    """Successful turn result."""

    result: AgentResult


@dataclass(slots=True)
class TurnFailure:
    """Failed turn result."""

    error: Exception
    messages: list[Message] | None = None
    conversation_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class TurnStopped:
    """Stopped turn result."""

    result: AgentResult | None = None
    messages: list[Message] | None = None
    conversation_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class InputInterrupted:
    """Sentinel for keyboard interruption during input."""


@dataclass(slots=True)
class PendingPrompt:
    """Queued or steering prompt waiting behind the active turn."""

    prompt: str
    kind: Literal["queued", "steering"] = "queued"
    user_message: Message | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    paused: bool = False

    def copy_for_queue(self) -> PendingPrompt:
        """Return a mutable copy preserving queue metadata."""
        return PendingPrompt(
            self.prompt,
            kind=self.kind,
            user_message=self.user_message,
            id=self.id,
            created_at=self.created_at,
            paused=self.paused,
        )


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """Interactive slash command metadata."""

    name: str
    description: str
    usage: str | None = None


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "/compact",
        "Summarize older conversation context into memory.",
    ),
    SlashCommand("/shortcuts", "Show interactive keyboard shortcuts."),
    SlashCommand("?", "Alias for /shortcuts."),
    SlashCommand("/info", "Show details about the current session."),
    SlashCommand("/new", "Start a fresh session in the current workspace."),
    SlashCommand("/fork", "Fork the current session and switch to it."),
    SlashCommand("/title", "Rename the active session.", "new-title"),
    SlashCommand("/tree", "Navigate the current session tree."),
    SlashCommand(
        "/model",
        "Open the model switcher.",
    ),
    SlashCommand(
        "/tools",
        "Toggle tools for this session, this root, or globally.",
    ),
    SlashCommand(
        "/mcp",
        "Manage MCP servers and tools for this session, repo, or globally.",
        "server",
    ),
    SlashCommand("/queue", "Open the interactive prompt queue manager."),
    SlashCommand("/image", "Attach an image file to the next prompt.", "path"),
    SlashCommand(
        "/skill",
        "Activate a discovered skill for this session.",
        "name",
    ),
)


@dataclass(slots=True)
class BasicCliState:
    """Mutable state for the basic interactive CLI loop."""

    messages: list[Message]
    pending_prompts: list[PendingPrompt]
    pending_images: list[ImageAttachment] = field(default_factory=list)
    worker: Thread | None = None
    active_stop_request: Event | None = None
    shutdown_requested: bool = False
    input_closed: bool = False
    exit_notice_emitted: bool = False


@dataclass(slots=True)
class PromptCliState:
    """Mutable state for the prompt-toolkit interactive loop."""

    messages: list[Message]
    pending_prompts: list[PendingPrompt]
    pending_images: list[ImageAttachment] = field(default_factory=list)
    worker: Thread | None = None
    active_stop_request: Event | None = None
    active_user_message: Message | None = None
    active_turn_id: int = 0
    abandoned_turn_ids: set[int] | None = None
    steered_turn_ids: set[int] | None = None
    shutdown_requested: bool = False
    exit_notice_emitted: bool = False
    status_message: str = ""
    submit_action: str = "steer"
    context_usage_text: str | None = None
    context_usage_percent: int | None = None
    context_input_tokens: int | None = None
    context_max_tokens: int | None = None
    turn_start_time: float | None = None
    turn_tool_count: int = 0
    turn_input_tokens: int | None = None
    turn_output_tokens: int | None = None
    turn_reasoning_tokens: int | None = None
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_tool_calls: int = 0
    spinner_index: int = 0
    thinking_effort: str | None = None
    next_editor_text: str | None = None


def prompt_turn_tracking(
    state: PromptCliState,
) -> tuple[set[int], set[int]]:
    """Return abandoned and steered turn tracking sets."""
    abandoned = state.abandoned_turn_ids
    steered = state.steered_turn_ids
    if abandoned is None or steered is None:
        raise RuntimeError("Prompt CLI turn tracking state is not initialized")
    return abandoned, steered


def partial_messages_from_error(error: Exception) -> list[Message] | None:
    """Extract sanitized partial messages from a provider/runtime exception."""
    messages = getattr(error, "partial_messages", None)
    if not isinstance(messages, list):
        return None
    if not all(isinstance(message, Message) for message in messages):
        return None
    return _drop_incomplete_tool_turns(messages)


def partial_conversation_entries_from_error(
    error: Exception,
) -> list[ConversationEntry] | None:
    """Extract partial structured entries from a provider/runtime exception."""
    entries = getattr(error, "partial_conversation_entries", None)
    if not isinstance(entries, list):
        return None
    if not all(isinstance(entry, ConversationEntry) for entry in entries):
        return None
    return [entry.model_copy(deep=True) for entry in entries]


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
    """Extract usage percent, input tokens, and max tokens from a usage payload."""
    if usage is None:
        return {"usage_percent": None, "input_tokens": None, "max_tokens": None}
    usage_percent = usage.get("usage_percent")
    input_tokens = usage.get("input_tokens")
    max_tokens = usage.get("max_total_tokens")
    return {
        "usage_percent": usage_percent if isinstance(usage_percent, int) else None,
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "max_tokens": max_tokens if isinstance(max_tokens, int) else None,
    }


def estimate_context_usage_text(
    agent: AgentRunner,
    prompt: str,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> str | None:
    """Estimate remaining context budget as toolbar text."""
    usage = estimate_context_usage(
        agent,
        prompt,
        messages,
        conversation_entries=conversation_entries,
    )
    return format_context_usage_text(usage)
