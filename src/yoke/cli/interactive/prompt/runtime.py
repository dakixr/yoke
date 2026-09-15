"""Isolated turn construction and cleanup under workspace leases."""

from __future__ import annotations

from contextlib import ExitStack
from threading import Thread

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.in_process_tool import wait_for_in_process_tools
from yoke.agent.models import ConversationEntry, Message
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.runtime.workspaces import retain_turn_workspace, take_turn_workspace
from yoke.session.workspace import WorkspaceConflict
from yoke.session.workspace import require_session_workspace, require_workspace
from yoke.session.workspace import workspace_lease


def prepare_turn_agent(
    agent: AgentRunner,
    *,
    messages: list[Message],
    entries: list[ConversationEntry],
    active_session: ActiveSession | None = None,
) -> AgentRunner:
    """Fork mutable state, retaining ownership even if loading it fails."""
    if not isinstance(agent, RuntimeAgent):
        return agent
    with ExitStack() as leases:
        if active_session is not None:
            leases.enter_context(
                workspace_lease(active_session.store, active_session.id)
            )
            record = active_session.store.load(active_session.id)
            if active_session.store.exists(active_session.id):
                require_session_workspace(record)
                if record.root != str(active_session.root):
                    raise WorkspaceConflict(active_session.id, record.root)
            require_workspace(active_session.root, session_id=active_session.id)
        turn_agent = None
        try:
            turn_agent = agent.fork(isolate_provider=True, include_state=False)
            # Loading may start resources before raising. Transfer the lease now
            # so an unreturned runtime's reaper retains it through physical cleanup.
            retain_turn_workspace(turn_agent, leases)
            if entries:
                turn_agent.load_owned_conversation(
                    entries,
                    available_skills=agent.available_skills,
                    active_skills=agent.active_skills,
                )
            else:
                turn_agent.load_conversation(
                    messages=messages,
                    available_skills=agent.available_skills,
                    active_skills=agent.active_skills,
                )
            if active_session is not None:
                require_workspace(active_session.root, session_id=active_session.id)
        except BaseException as exc:
            retire_turn_agent(turn_agent, primary_agent=agent)
            if isinstance(exc, ValueError) and active_session is not None:
                # Runtime binding can race directory deletion after admission.
                # Preserve unrelated configuration/load errors when it is valid.
                require_workspace(active_session.root, session_id=active_session.id)
            raise
        return turn_agent


def retire_turn_agent(
    turn_agent: AgentRunner | None,
    *,
    primary_agent: AgentRunner,
) -> None:
    """Release an isolated turn runtime away from the control path."""
    if not isinstance(turn_agent, RuntimeAgent) or turn_agent is primary_agent:
        return
    tool_map = turn_agent.tools
    provider = turn_agent.provider

    def release() -> None:
        with take_turn_workspace(turn_agent):
            try:
                wait_for_in_process_tools(tool_map)
                turn_agent.close()
            finally:
                close = getattr(provider, "close", None)
                if callable(close) and provider is not getattr(
                    primary_agent, "provider", None
                ):
                    close()

    Thread(target=release, daemon=True, name="yoke-turn-reaper").start()
