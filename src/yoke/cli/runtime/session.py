"""Session persistence helpers for yoke CLI runtime."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from threading import Thread

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import AgentState
from yoke.agent.state import capture_agent_state
from yoke.agent.state import merge_conversation_branch
from yoke.cli.config.args import CLIArgs
from yoke.cli.providers.state import apply_session_provider_defaults
from yoke.cli.providers.state import capture_provider_session_state
from yoke.cli.providers.state import provider_session_state_from_values
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.base import AgentRunner
from yoke.cli.runtime.session_selector import (
    _can_use_keyboard_session_selector,
)
from yoke.cli.runtime.session_selector import _format_session_activity
from yoke.cli.runtime.session_selector import (
    _select_session_id_interactive,
)
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore
from yoke.cli.session import fallback_session_title
from yoke.cli.session import new_session_id
from yoke.cli.session_tree import entries_preserve_active_prefix


def create_active_session(args: CLIArgs, *, root: Path) -> ActiveSession:
    """Create or load the active session for a CLI invocation."""
    store = SessionStore()
    session_id = args.session or new_session_id()
    record = store.load(session_id)
    resolved_root = root.resolve()
    if record.created_at is None:
        store.save(
            session_id,
            record.messages,
            root=resolved_root,
            title=record.title,
            provider_name=record.provider_name,
            model_id=record.model_id,
            reasoning_effort=record.reasoning_effort,
            context_window_tokens=record.context_window_tokens,
        )
        record = store.load(session_id)
    return ActiveSession(
        id=session_id,
        root=Path(record.root).resolve() if record.root else resolved_root,
        store=store,
        record=record,
        title=record.title,
    )


def ensure_session_title(
    active_session: ActiveSession,
    agent: AgentRunner,
    messages: list[Message] | str,
    *,
    stderr: OutputStream | None = None,
) -> None:
    """Assign a generated title to the session when needed."""
    del stderr
    if active_session.title:
        return
    if isinstance(messages, str):
        active_session.title = generate_session_title(agent, messages)
    else:
        active_session.title = generate_session_title_from_messages(agent, messages)
    save_active_session(
        active_session,
        active_session.record.messages,
        conversation_entries=active_session.record.conversation_entries,
        leaf_id=active_session.record.leaf_id,
    )


def generate_session_title(agent: AgentRunner, prompt: str) -> str:
    """Generate a short session title."""
    return generate_session_title_from_messages(agent, [Message.user(prompt)])


def generate_session_title_from_messages(
    agent: AgentRunner,
    messages: list[Message],
) -> str:
    """Generate a short session title from the current conversation context."""
    provider = getattr(agent, "provider", None)
    fallback_prompt = _fallback_title_prompt(messages)
    if provider is None or any(message.has_image_inputs() for message in messages):
        return fallback_session_title(fallback_prompt)
    try:
        response = provider.complete(
            [
                Message.system(
                    "Create a concise title, 6 words or fewer, for this "
                    "conversation. Return only the title."
                ),
                *[message.model_copy(deep=True) for message in messages],
            ],
            [],
        )
    except Exception:
        return fallback_session_title(fallback_prompt)
    title = (response.plain_text_content or "").strip().strip("\"'")
    return fallback_session_title(title or fallback_prompt)


def start_session_title_generation(
    active_session: ActiveSession,
    agent: AgentRunner,
    messages: list[Message],
    *,
    on_done: Callable[[], None] | None = None,
) -> Thread | None:
    """Generate the first session title in a background thread."""
    if active_session.title:
        return None
    message_snapshot = [message.model_copy(deep=True) for message in messages]

    def generate_and_save() -> None:
        if active_session.title:
            return
        title = generate_session_title_from_messages(agent, message_snapshot)
        if active_session.title:
            return
        active_session.title = title
        save_active_session(
            active_session,
            active_session.record.messages,
            conversation_entries=active_session.record.conversation_entries,
            leaf_id=active_session.record.leaf_id,
            agent=agent,
        )
        if on_done is not None:
            on_done()

    thread = Thread(target=generate_and_save, daemon=True)
    thread.start()
    return thread


def _fallback_title_prompt(messages: list[Message]) -> str:
    for message in messages:
        if message.role == "user":
            text = message.text_content()
            if text:
                return text
    text_parts = [message.text_content() or "" for message in messages]
    return " ".join(part for part in text_parts if part)


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
) -> None:
    """Write the current session state to storage."""
    if conversation_entries is not None:
        if not entries_preserve_active_prefix(
            active_session.record,
            conversation_entries,
        ):
            conversation_entries, merged_leaf_id = merge_conversation_branch(
                active_session.record.conversation_entries,
                conversation_entries,
            )
            leaf_id = leaf_id or merged_leaf_id
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
    active_session.store.save(
        active_session.id,
        messages,
        conversation_entries=conversation_entries,
        leaf_id=leaf_id,
        active_skills=active_session.record.active_skills,
        skill_dirs=active_session.record.skill_dirs,
        root=active_session.root,
        title=active_session.title,
        provider_name=provider_state.provider_name,
        model_id=provider_state.model_id,
        reasoning_effort=provider_state.reasoning_effort,
        context_window_tokens=provider_state.context_window_tokens,
    )
    active_session.record = active_session.store.load(active_session.id)


def save_agent_session_state(
    active_session: ActiveSession,
    state: AgentState,
    *,
    leaf_id: str | None = None,
    agent: object | None = None,
) -> None:
    """Write captured agent session state to storage."""
    if state.active_skills is not None:
        active_session.record.active_skills = state.active_skills
    if state.skill_dirs is not None:
        active_session.record.skill_dirs = state.skill_dirs
    save_active_session(
        active_session,
        state.messages,
        conversation_entries=state.conversation_entries,
        leaf_id=leaf_id,
        agent=agent,
    )


def persist_session_state(
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None = None,
) -> None:
    """Sync skill state and persist the active session."""
    state = capture_agent_state(
        agent,
        messages=messages,
        conversation_entries=conversation_entries,
    )
    save_agent_session_state(
        active_session,
        state,
        agent=agent,
    )


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


def select_latest_session_id(
    store: SessionStore,
    *,
    root: Path,
    all_sessions: bool = False,
) -> str:
    """Return the most recently updated saved session id."""
    records = store.list(root=None if all_sessions else root)
    if not records:
        if all_sessions:
            raise ValueError("No saved sessions found.")
        raise ValueError(f"No sessions found for root: {root.resolve()}")
    return records[0].id


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


def _format_session_choice(record: SessionRecord) -> str:
    title = record.title or "Untitled session"
    updated = _format_session_activity(record)
    return f"{title}  {updated}  {record.id}"
