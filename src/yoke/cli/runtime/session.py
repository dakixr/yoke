"""Session persistence helpers for yoke CLI runtime."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import AgentState
from yoke.agent.state import capture_agent_state
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.state import transcript_messages_from_entries
from yoke.cli.config import CLIArgs
from yoke.cli.providers.state import apply_session_provider_defaults
from yoke.cli.providers.state import capture_provider_session_state
from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.providers.state import provider_session_state_from_values
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.selector.session import (
    _can_use_keyboard_session_selector,
)
from yoke.cli.runtime.selector.session import _format_session_activity
from yoke.cli.runtime.selector.session import (
    _select_session_id_interactive,
)
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore
from yoke.cli.session import fallback_session_title
from yoke.cli.session import new_session_id
from yoke.cli.session.metadata import update_loaded_provider_state
from yoke.cli.runtime.title import generate_session_title
from yoke.cli.runtime.title import session_usage_metric_context


def create_active_session(args: CLIArgs, *, root: Path) -> ActiveSession:
    """Create or load the active session for a CLI invocation."""
    store = SessionStore()
    has_explicit_session = args.session is not None
    if args.fork_session_id is not None:
        record = store.fork(args.fork_session_id, root=root.resolve())
        return ActiveSession(
            id=record.id,
            root=Path(record.root).resolve() if record.root else root.resolve(),
            store=store,
            record=record,
            title=record.title,
        )
    session_id = args.session or new_session_id()
    record = store.load(session_id)
    resolved_root = root.resolve()
    if record.created_at is None and has_explicit_session:
        record = store.save(
            session_id,
            record.messages,
            root=resolved_root,
            title=record.title,
            provider_name=record.provider_name,
            model_id=record.model_id,
            reasoning_effort=record.reasoning_effort,
            context_window_tokens=record.context_window_tokens,
        )
    return ActiveSession(
        id=session_id,
        root=Path(record.root).resolve() if record.root else resolved_root,
        store=store,
        record=record,
        title=record.title,
    )


def fork_active_session(
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
    *,
    title: str | None = None,
) -> ActiveSession:
    """Persist and switch to a fork of the current active session."""
    persist_session_state(active_session, agent, messages)
    forked_record = active_session.store.fork(
        active_session.id,
        root=active_session.root,
        title=title,
    )
    return ActiveSession(
        id=forked_record.id,
        root=Path(forked_record.root).resolve()
        if forked_record.root
        else active_session.root,
        store=active_session.store,
        record=forked_record,
        title=forked_record.title,
    )


def ensure_session_title(
    active_session: ActiveSession,
    agent: object,
    prompt: str,
) -> None:
    """Generate and persist a title for an unnamed session."""
    if active_session.title:
        return
    messages = active_session.messages()
    if not messages or messages[-1].plain_text_content != prompt:
        messages.append(Message.user(prompt))
    with session_usage_metric_context(active_session, prompt):
        generated = generate_session_title(agent, messages)
    active_session.title = generated or fallback_session_title(prompt)
    save_active_session(
        active_session,
        active_session.messages(),
        conversation_entries=active_session.active_entries(),
        leaf_id=active_session.record.leaf_id,
    )


def sync_agent_skill_state_to_session(
    active_session: ActiveSession,
    agent: object,
) -> None:
    """Persist active skill state from an Agent implementation."""
    state = capture_agent_state(agent)
    if state.active_skills is not None:
        active_session.record.active_skills = state.active_skills
    if state.skill_dirs is not None:
        active_session.record.skill_dirs = state.skill_dirs


def save_active_session(
    active_session: ActiveSession,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None = None,
    leaf_id: str | None = None,
    agent: object | None = None,
    active_skills: list[ActiveSkill] | None = None,
    skill_dirs: list[str] | None = None,
) -> None:
    """Write the current session state to storage."""
    with active_session.save_lock:
        _save_active_session_locked(
            active_session,
            messages,
            conversation_entries=conversation_entries,
            leaf_id=leaf_id,
            agent=agent,
            active_skills=active_skills,
            skill_dirs=skill_dirs,
        )


def _save_active_session_locked(
    active_session: ActiveSession,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None,
    leaf_id: str | None,
    agent: object | None,
    active_skills: list[ActiveSkill] | None,
    skill_dirs: list[str] | None,
) -> None:
    provider_state = (
        capture_provider_session_state(agent)
        if agent is not None
        else provider_session_state_from_values(
            provider_name=active_session.record.provider_name,
            model_id=active_session.record.model_id,
            reasoning_effort=active_session.record.reasoning_effort,
            context_window_tokens=active_session.record.context_window_tokens,
        )
    )
    active_session.record = active_session.store.save(
        active_session.id,
        messages,
        conversation_entries=conversation_entries,
        leaf_id=leaf_id,
        active_skills=(
            active_session.record.active_skills
            if active_skills is None
            else active_skills
        ),
        skill_dirs=(
            active_session.record.skill_dirs if skill_dirs is None else skill_dirs
        ),
        root=active_session.root,
        title=active_session.title,
        provider_name=provider_state.provider_name,
        model_id=provider_state.model_id,
        reasoning_effort=provider_state.reasoning_effort,
        context_window_tokens=provider_state.context_window_tokens,
        existing_record=active_session.record,
        tree_index=active_session.tree_index,
    )


def save_active_session_metadata(
    active_session: ActiveSession,
    provider_state: ProviderSessionState,
) -> None:
    """Persist provider metadata without processing conversation state."""
    with active_session.save_lock:
        active_session.record = update_loaded_provider_state(
            active_session.store,
            active_session.record,
            root=active_session.root,
            title=active_session.title,
            provider_name=provider_state.provider_name,
            model_id=provider_state.model_id,
            reasoning_effort=provider_state.reasoning_effort,
            context_window_tokens=provider_state.context_window_tokens,
        )


def save_agent_session_state(
    active_session: ActiveSession,
    state: AgentState,
    *,
    leaf_id: str | None = None,
    agent: object | None = None,
) -> None:
    """Write captured agent session state to storage."""
    save_active_session(
        active_session,
        state.messages if state.conversation_entries is None else [],
        conversation_entries=state.conversation_entries,
        leaf_id=leaf_id,
        agent=agent,
        active_skills=state.active_skills,
        skill_dirs=state.skill_dirs,
    )


def persist_session_state(
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None = None,
) -> None:
    """Sync skill state and persist the active session."""
    state = _state_matching_transcript(
        active_session,
        agent,
        messages,
        conversation_entries=conversation_entries,
    )
    try:
        save_agent_session_state(
            active_session,
            state,
            agent=agent,
        )
    except OSError as exc:
        print(
            f"Warning: failed to persist yoke session {active_session.id}: {exc}",
            file=sys.stderr,
        )


def _state_matching_transcript(
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None,
) -> AgentState:
    """Capture structured state without overriding a newer transcript."""
    if conversation_entries is not None:
        return _state_with_borrowed_conversation(agent, conversation_entries)
    if active_session.messages() == messages:
        return _state_with_borrowed_conversation(
            agent, active_session.active_entry_refs()
        )
    agent_state = capture_agent_state(agent)
    if agent_state.messages == messages:
        return agent_state
    session_branch = active_session.active_entries()
    matching_entries = (
        session_branch
        if transcript_messages_from_entries(session_branch) == messages
        else conversation_entries_from_messages(messages)
    )
    return capture_agent_state(
        agent,
        conversation_entries=matching_entries,
    )


def _state_with_borrowed_conversation(
    agent: object,
    conversation_entries: list[ConversationEntry],
) -> AgentState:
    """Capture agent metadata while borrowing a synchronous save snapshot."""
    state = capture_agent_state(agent, conversation_entries=[])
    state.conversation_entries = conversation_entries
    return state


def apply_session_defaults_to_args(
    args: CLIArgs,
    record: SessionRecord,
) -> None:
    """Apply persisted provider/model defaults from a session record."""
    apply_session_provider_defaults(
        args,
        provider_session_state_from_values(
            provider_name=record.provider_name,
            model_id=record.model_id,
            reasoning_effort=record.reasoning_effort,
            context_window_tokens=record.context_window_tokens,
        ),
    )


def select_session_id(
    store: SessionStore,
    *,
    root: Path,
    all_sessions: bool = False,
    input_func: Callable[..., str],
    stdout: OutputStream | None = None,
) -> str:
    """Prompt the user to select a saved session."""
    records = store.list(root=None if all_sessions else root)
    if not records:
        if all_sessions:
            raise ValueError("No saved sessions found.")
        raise ValueError(f"No sessions found for root: {root.resolve()}")
    console = build_console(stdout or sys.stdout)
    if _can_use_keyboard_session_selector(stdout or sys.stdout):
        selected = _select_session_id_interactive(
            records,
            root=root,
            all_sessions=all_sessions,
            set_pinned=store.set_pinned,
        )
        if selected is None:
            raise ValueError("Session selection cancelled.")
        return selected
    return _select_session_id_by_number(
        records,
        input_func=input_func,
        console=console,
        all_sessions=all_sessions,
    )


def _select_session_id_by_number(
    records: list[SessionRecord],
    *,
    input_func: Callable[..., str],
    console,
    all_sessions: bool,
) -> str:
    """Prompt for a session with the portable numeric fallback."""
    heading = "Select a session to resume:"
    if all_sessions:
        heading = f"{heading} (all roots)"
    console.print(heading)
    for index, record in enumerate(records, start=1):
        title = record.title or "Untitled session"
        updated = _format_session_activity(record)
        console.print(f"{index}. {title} ({record.id}) {updated}")
    raw = input_func("Session number: ").strip()
    try:
        selected = int(raw)
    except ValueError as exc:
        raise ValueError("Session selection must be a number.") from exc
    if selected < 1 or selected > len(records):
        raise ValueError("Session selection is out of range.")
    return records[selected - 1].id
