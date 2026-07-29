"""Session slash-command helpers."""

from __future__ import annotations

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
    )
    active_session.title = active_session.record.title
    persist_session_state(active_session, agent, messages)
    state = "pinned" if active_session.record.pinned else "unpinned"
    return f"Session {state}: {active_session.id}"


def print_session_info(
    console,
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
) -> None:
    """Print active session metadata in scrollback."""
    record = active_session.record
    provider = record.provider_name or _agent_provider_name(agent) or "unknown"
    model = record.model_id or _agent_model_id(agent) or "unknown"
    lines = [
        "Session info:",
        f"Session id: {active_session.id}",
        f"Title: {active_session.title or record.title or 'Untitled session'}",
        f"Pinned: {_yes_no(record.pinned)}",
        f"Root: {active_session.root}",
        f"Path: {active_session.store.path_for(active_session.id)}",
        f"Provider: {provider}",
        f"Model: {model}",
        f"Messages: {len(messages)}",
        f"Conversation entries: {len(record.conversation_entries)}",
        f"Created: {record.created_at or 'unknown'}",
        f"Updated: {record.updated_at or 'unknown'}",
        f"Reasoning effort: {record.reasoning_effort or 'unknown'}",
        f"Context window: {_context_window_text(record.context_window_tokens)}",
    ]
    if record.leaf_id:
        lines.append(f"Leaf id: {record.leaf_id}")
    print_scrollback_notice(console, "\n".join(lines))


def _agent_provider_name(agent: object) -> str | None:
    provider = getattr(agent, "provider", None)
    name = getattr(provider, "name", None)
    if isinstance(name, str) and name:
        return name
    if provider is not None:
        return provider.__class__.__name__
    return None


def _agent_model_id(agent: object) -> str | None:
    provider = getattr(agent, "provider", None)
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    if isinstance(model, str) and model:
        return model
    model_id = getattr(provider, "model_id", None)
    if isinstance(model_id, str) and model_id:
        return model_id
    return None


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _context_window_text(tokens: int | None) -> str:
    if tokens is None:
        return "unknown"
    return f"{tokens} tokens"
