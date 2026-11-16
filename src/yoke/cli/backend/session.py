"""Session operations shared by the basic and prompt-toolkit front ends.

Only this adapter and its backend collaborators know about the in-process
runtime. Route mappings are documented on each operation. The HTTP adapter in
Phase 5 can keep terminal rendering and key handling independent of that runtime.
"""

from __future__ import annotations

from collections.abc import Sequence, Callable
import sqlite3
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import SkillSpec
from yoke.agent.provider_selection import ProviderSessionState
from yoke.cli.backend.catalog import ToolInventory, McpInventory
from yoke.cli.runtime.compaction import CompactedHistory
from yoke.cli.runtime.tree import TreeNode, TreeNavigationResult
from yoke.agent.state import capture_agent_state
from yoke.cli.backend.events import EventSubscriptions
from yoke.cli.backend.inspection import ProcessInspection
from yoke.cli.config import CLIArgs, format_provider_model_status
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.compaction import estimate_context_usage, force_compact_history
from yoke.cli.runtime.session import (
    create_active_session,
    fork_active_session,
    persist_session_state,
    sync_agent_skill_state_to_session,
)
from yoke.cli.runtime.title import (
    generate_session_title,
    session_usage_metric_context,
)
from yoke.cli.runtime.tree import (
    get_session_tree,
    navigate_session_tree,
    set_entry_label,
)

if TYPE_CHECKING:
    from yoke.cli.backend.turns import SubmittedTurn, Checkpoint
    from yoke.cli.backend.models import PendingPrompt


class SessionBackend:
    """One CLI session's runtime operations, with no terminal dependencies.

    ``wrap`` accepts the old injected-agent entry points during this preparatory
    refactor. UI code passes the resulting backend onward. Test doubles remain
    accepted at those entry points; the production runtime is RuntimeAgent.
    """

    def __init__(self, agent: object, session: ActiveSession | None = None) -> None:
        self._agent = agent
        self._session = session
        self.events = EventSubscriptions()

    @classmethod
    def wrap(
        cls, agent: object, session: ActiveSession | None = None
    ) -> SessionBackend:
        """Adapt a CLI entry point once, or reuse its existing session backend."""
        from yoke.cli.backend.daemon import DaemonActiveSession, DaemonSessionBackend

        if isinstance(agent, DaemonSessionBackend):
            if isinstance(session, DaemonActiveSession):
                agent.select_session(session)
            return cast(SessionBackend, agent)
        if isinstance(agent, cls):
            if session is not None:
                agent._session = session
            return agent
        return cls(agent, session)

    @property
    def session(self) -> ActiveSession:
        """GET /session/{id}: the currently selected session snapshot."""
        if self._session is None:
            raise ValueError("This operation needs an active session.")
        return self._session

    @property
    def runtime_available(self) -> bool:
        return isinstance(self._agent, RuntimeAgent)

    def provider_label(self) -> str | None:
        """GET /session/{id}: formatted provider/model status."""
        return format_provider_model_status(self._agent)

    def thinking(self) -> tuple[str | None, tuple[str, ...]]:
        """GET /session/{id} and GET /model: effort and available levels."""
        provider = getattr(self._agent, "provider", None)
        effort = getattr(getattr(provider, "config", None), "reasoning_effort", None)
        info_reader = getattr(provider, "current_model_info", None)
        info = info_reader() if callable(info_reader) else None
        return (
            effort if isinstance(effort, str) and effort.strip() else None,
            tuple(getattr(info, "thinking_levels", ())),
        )

    def set_thinking(self, effort: str) -> None:
        """POST /session/{id}/selection with the current model and new effort."""
        config = getattr(getattr(self._agent, "provider", None), "config", None)
        if config is not None and hasattr(config, "reasoning_effort"):
            config.reasoning_effort = effort

    def available_skills(self) -> Sequence[SkillSpec]:
        """GET /skill: discovered skills for completion."""
        return getattr(self._agent, "available_skills", ())

    def context_usage(
        self,
        prompt: str,
        messages: list[Message],
        *,
        conversation_entries: Sequence[ConversationEntry] | None = None,
    ) -> dict[str, int] | None:
        """GET /session/{id}/context plus GET /event context updates.

        Draft estimation is local today; the current HTTP context route exports
        context but has no editor-draft estimate parameter yet.
        """
        return estimate_context_usage(
            self._agent,
            prompt,
            messages,
            conversation_entries=conversation_entries,
            take_entry_ownership=isinstance(conversation_entries, list),
        )

    def persist(
        self,
        messages: list[Message],
        *,
        conversation_entries: list[ConversationEntry] | None = None,
        turn_agent: object | None = None,
    ) -> None:
        """In-process checkpoint; HTTP clients read GET /session/{id}/message."""
        persist_session_state(
            self.session,
            turn_agent or self._agent,
            messages,
            conversation_entries=conversation_entries,
        )

    def sync_skills(self) -> None:
        """In-process checkpoint; HTTP runtime owns this on POST prompt."""
        sync_agent_skill_state_to_session(self.session, self._agent)

    def checkpoint_skills(self, turn_agent: object) -> None:
        """Keep accepted checkpoint skill activation on the primary runtime."""
        if isinstance(self._agent, RuntimeAgent) and isinstance(
            turn_agent, RuntimeAgent
        ):
            self._agent.active_skills = [
                skill.model_copy(deep=True) for skill in turn_agent.active_skills
            ]

    def promote_turn(self, turn_agent: object) -> object:
        """Accept a turn's runtime state; HTTP runtime owns POST prompt completion."""
        from yoke.session.workspace import promote_session_turn

        if isinstance(self._agent, RuntimeAgent) and isinstance(
            turn_agent, RuntimeAgent
        ):
            if turn_agent is not self._agent:
                promote_session_turn(self._agent, turn_agent)
            return self._agent
        return turn_agent

    def retire_turn(self, turn_agent: object | None) -> None:
        """Release an abandoned/completed worker; HTTP runtime owns cleanup."""
        from yoke.cli.runtime.turn_agent import retire_turn_agent

        retire_turn_agent(turn_agent, primary_agent=self._agent)

    def submit(
        self,
        prompt: str,
        messages: list[Message],
        *,
        entries: list[ConversationEntry],
        stop_event: Event,
        user_message: Message | None,
        checkpoint: Checkpoint,
        starting_prompt: tuple[PendingPrompt, int] | None = None,
        delivery: str = "queue",
    ) -> SubmittedTurn:
        """POST /session/{id}/prompt; iterate GET /event, then POST wait.

        Delivery is steer or queue. Existing front-end scheduling admits work
        before reaching this in-process execution adapter during preparation.
        """
        from yoke.cli.backend.turns import SubmittedTurn, TurnRunner

        if delivery not in {"steer", "queue"}:
            raise ValueError(f"Unknown prompt delivery: {delivery}")
        return SubmittedTurn(
            TurnRunner(self._agent, self.session),
            prompt,
            messages,
            entries=entries,
            stop_event=stop_event,
            user_message=user_message,
            checkpoint=checkpoint,
            starting_prompt=starting_prompt,
            subscriptions=self.events,
        )

    @staticmethod
    def interrupt(stop_event: Event) -> None:
        """POST /session/{id}/interrupt."""
        stop_event.set()
        backend = getattr(stop_event, "daemon_backend", None)
        if backend is not None:
            backend.interrupt(stop_event)

    def persist_metadata(self, reasoning_effort: str | None) -> None:
        """POST /session/{id}/selection: persist effort without loading messages."""
        from yoke.cli.runtime.metadata import persist_active_session_metadata

        persist_active_session_metadata(
            self.session, self._agent, reasoning_effort=reasoning_effort
        )

    @staticmethod
    def require_workspace(session: ActiveSession) -> None:
        """Admission validation before POST prompt or a session-changing command."""
        from yoke.session.workspace import require_workspace

        require_workspace(session.root, session_id=session.id)

    @staticmethod
    def interrupt_snapshot(
        *,
        messages: Sequence[Message],
        entries: Sequence[ConversationEntry],
        user_message: Message | None,
    ) -> tuple[list[Message], list[ConversationEntry]]:
        """POST interrupt: project the accepted continuation without agent access."""
        from yoke.session.interrupt import interrupted_turn_snapshot

        return interrupted_turn_snapshot(
            messages=messages, entries=entries, user_message=user_message
        )

    @staticmethod
    def submission_context[T](callback: Callable[[], T]) -> Callable[[], T]:
        """Retain workspace ownership across the terminal's executor boundary."""
        from yoke.cli.runtime.workspaces import cli_workspaces

        owner = cli_workspaces.get()

        def run() -> T:
            token = cli_workspaces.set(owner)
            try:
                return callback()
            finally:
                cli_workspaces.reset(token)

        return run

    def compact(
        self,
        messages: list[Message],
        *,
        conversation_entries: Sequence[ConversationEntry] | None = None,
    ) -> CompactedHistory:
        """POST /session/{id}/compact, preserving the established projection."""
        with session_usage_metric_context(self.session, ""):
            return force_compact_history(
                self._agent, messages, conversation_entries=conversation_entries
            )

    def pin(self, messages: list[Message]) -> bool:
        """PATCH /session/{id} with pinned."""
        session = self.session
        if session.record.created_at is None:
            self.persist(messages)
        session.record = session.store.set_pinned(
            session.id, not session.record.pinned, existing_record=session.record
        )
        session.title = session.record.title
        return session.record.pinned

    def refresh_session(self) -> None:
        """In-process metadata is updated by persistence itself."""

    def conversation_entry_count(self) -> int:
        return len(self.session.record.conversation_entries)

    def database_path(self) -> Path:
        """Local storage location for the in-process /info oracle."""
        return self.session.store.database_path

    def title(self, title: str, messages: list[Message]) -> None:
        """PATCH /session/{id} with title."""
        from yoke.session.repository import fallback_session_title

        self.session.title = fallback_session_title(title)
        self.persist(messages)

    def regenerate_title(self, messages: list[Message]) -> str | None:
        """POST /session/{id}/title/regenerate."""
        with session_usage_metric_context(self.session, ""):
            title = generate_session_title(self._agent, messages)
        if title is not None:
            self.session.title = title
            self.persist(messages)
        return title

    def fork(self, messages: list[Message]) -> ActiveSession:
        """POST /session/{id}/fork."""
        return fork_active_session(self.session, self._agent, messages)

    def new(self) -> ActiveSession:
        """POST /session, inheriting the current workspace."""
        session = create_active_session(
            CLIArgs(root=str(self.session.root)), root=self.session.root
        )
        reset = getattr(self._agent, "reset", None)
        if callable(reset):
            reset()
        persist_session_state(session, self._agent, [], conversation_entries=[])
        return session

    def tree(self) -> list[TreeNode]:
        """GET /session/{id}/tree."""
        return get_session_tree(self.session)

    def label_entry(self, entry_id: str, label: str) -> None:
        """PATCH /session/{id}/tree/{entry_id}."""
        set_entry_label(self.session, entry_id, label)

    def navigate(
        self,
        entry_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> TreeNavigationResult:
        """POST /session/{id}/tree/navigate."""
        with session_usage_metric_context(self.session, ""):
            return navigate_session_tree(
                self.session,
                self._agent,
                entry_id,
                summarize=summarize,
                custom_instructions=custom_instructions,
            )

    def tools(self) -> ToolInventory:
        """GET /tool and PATCH /session/{id}/tool."""
        return ToolInventory(self._agent)

    def mcp(self, root: Path) -> McpInventory:
        """GET/PATCH /session/{id}/mcp."""
        return McpInventory(self._agent, root)

    def processes(self) -> ProcessInspection:
        """GET /process?sessionID= and GET /agent-run?sessionID={id}."""
        from yoke.agent.tools.command_process import CommandProcessManager

        manager = getattr(self._agent, "command_process_manager", None)
        return ProcessInspection(
            manager if isinstance(manager, CommandProcessManager) else None
        )

    def activate_skill(self, name: str, messages: list[Message]) -> str | None:
        """POST /session/{id}/skill/{name}/activate. Return a notice only on failure."""
        from yoke.agent.session_tree import SessionTree
        from yoke.agent.skills.activation import activate_skills

        agent = self._agent
        if not isinstance(agent, RuntimeAgent) or agent.skill_registry is None:
            return "No skills are available in this session."
        activation = activate_skills(
            registry=agent.skill_registry,
            active_skills=agent.active_skills,
            names=[name],
        )
        if activation.missing:
            return f"Unknown skill: {name}"
        agent.active_skills = activation.active_skills
        tree = SessionTree.import_legacy(
            self.session.record.conversation_entries, self.session.record.leaf_id
        )
        tree.append_active_skills(activation.activated_skills)
        exported = tree.export_for_persistence()
        active = tree.export_active_for_persistence()
        agent.load_conversation(
            conversation_entries=list(active.entries),
            active_skills=activation.active_skills,
        )
        self.persist(messages, conversation_entries=list(exported.entries))
        return None

    @staticmethod
    def models(args: CLIArgs):
        """GET /model and GET /provider: available provider/model choices."""
        from yoke.cli.providers.catalog import list_all_provider_model_choices

        return list_all_provider_model_choices(args=args)

    def switch_model(
        self,
        qualified_model_id: str,
        reasoning_effort: str | None,
        *,
        args: CLIArgs,
        messages: list[Message],
    ) -> tuple[ProviderSessionState, list[Message], OSError | sqlite3.Error | None]:
        """POST /session/{id}/selection; return state, transcript and metadata error."""
        from yoke.cli.providers.state import switch_agent_provider_model
        from yoke.cli.runtime.session import save_active_session_metadata

        before = self._context_marker()
        provider_state = switch_agent_provider_model(
            self._agent,
            args=args,
            qualified_model_id=qualified_model_id,
            reasoning_effort=reasoning_effort,
        )
        error: OSError | sqlite3.Error | None = None
        if self._context_marker() == before:
            try:
                save_active_session_metadata(self.session, provider_state)
            except (OSError, sqlite3.Error) as exc:
                error = exc
            updated = messages
        else:
            state = capture_agent_state(self._agent)
            runtime_messages = getattr(self._agent, "messages", None)
            updated = (
                [message.model_copy(deep=True) for message in runtime_messages]
                if isinstance(runtime_messages, list)
                else state.messages
            )
            self.persist(updated, conversation_entries=state.conversation_entries)
        return provider_state, updated, error

    def _context_marker(self) -> tuple[int, str | None, str | None] | None:
        if not isinstance(self._agent, RuntimeAgent):
            return None
        return self._agent.conversation_marker
