"""Session slash-command helpers."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.models import Message
from yoke.cli.render import print_scrollback_notice
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import persist_session_state


def handle_pin_session(
    active_session: ActiveSession,
    agent: AgentRunner,
    messages: list[Message],
) -> str:
    """Toggle the active session pin state and return a status message."""
    if active_session.record.created_at is None:
        persist_session_state(active_session, agent, messages)
    pinned = not active_session.record.pinned
    active_session.record = active_session.store.set_pinned(
        active_session.id,
        pinned,
        existing_record=active_session.record,
    )
    active_session.title = active_session.record.title
    persist_session_state(active_session, agent, messages)
    state = "pinned" if active_session.record.pinned else "unpinned"
    return f"Session {state}: {active_session.id}"


def print_session_info(
    console,
    active_session: ActiveSession,
) -> None:
    """Print active session metadata in scrollback."""
    record = active_session.record
    lines = [
        "Session info:",
        f"Session id: {active_session.id}",
        f"Title: {record.title or 'Untitled session'}",
        f"Pinned: {_yes_no(record.pinned)}",
        f"Root: {active_session.root}",
        f"Path: {_session_path(active_session)}",
        f"Provider: {record.provider_name or 'unknown'}",
        f"Model: {record.model_id or 'unknown'}",
        f"Messages: {len(active_session.messages())}",
        f"Conversation entries: {len(record.conversation_entries)}",
        f"Created: {record.created_at or 'unknown'}",
        f"Updated: {record.updated_at or 'unknown'}",
        f"Reasoning effort: {record.reasoning_effort or 'unknown'}",
        f"Context window: {_context_window_text(record.context_window_tokens)}",
    ]
    print_scrollback_notice(console, "\n".join(lines))


def _session_path(active_session: ActiveSession) -> Path:
    return active_session.store.directory / f"{active_session.id}.jsonl"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _context_window_text(tokens: int | None) -> str:
    if tokens is None:
        return "unknown"
    return f"{tokens} tokens"
