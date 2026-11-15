"""Turn execution shared by the basic and prompt front ends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.state import capture_agent_state
from yoke.cli.config import RUN_ERRORS
from yoke.cli.interactive.common import (
    PendingPrompt,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
    partial_conversation_entries_from_error,
    partial_messages_from_error,
)
from yoke.cli.runtime import (
    ActiveSession,
    AgentRunner,
    EventRenderer,
    execute_turn,
    session_usage_metric_context,
    start_session_title_generation,
)
from yoke.cli.runtime.turn_agent import prepare_turn_agent
from yoke.session.workspace import (
    WorkspaceError,
    WorkspaceUnavailable,
    require_workspace,
)

type TurnOutcome = TurnSuccess | TurnFailure | TurnStopped
type Checkpoint = Callable[[AgentRunner, list[Message], list[ConversationEntry]], None]


@dataclass(slots=True)
class TurnRunner:
    """Prepare an isolated runtime and return a typed outcome to its owner."""

    agent: AgentRunner
    session: ActiveSession

    def run(
        self,
        prompt: str,
        messages: list[Message],
        *,
        entries: list[ConversationEntry],
        stop_event: Event,
        user_message: Message | None,
        indicator: EventRenderer,
        checkpoint: Checkpoint,
        starting_prompt: tuple[PendingPrompt, int] | None = None,
    ) -> TurnOutcome:
        turn_agent = self.agent
        execution_started = False
        try:
            require_workspace(self.session.root, session_id=self.session.id)
            turn_agent = prepare_turn_agent(
                self.agent,
                messages=messages,
                entries=entries,
                active_session=self.session,
            )
            start_session_title_generation(
                self.session,
                self.agent,
                prompt,
                messages=[*messages, user_message or Message.user(prompt)],
            )
            with session_usage_metric_context(self.session, prompt):
                require_workspace(self.session.root, session_id=self.session.id)
                execution_started = True
                result = execute_turn(
                    turn_agent,
                    prompt,
                    messages,
                    indicator=indicator,
                    stop_requested=stop_event.is_set,
                    user_message=user_message,
                    conversation_entries=entries,
                    after_tool_result_appended=lambda messages, entries: checkpoint(
                        turn_agent, messages, entries
                    ),
                )
            if result.status == "stopped":
                return TurnStopped(result=result, agent=turn_agent)
            return TurnSuccess(result=result, agent=turn_agent)
        except AgentStoppedError:
            snapshot = capture_agent_state(turn_agent)
            return TurnStopped(
                messages=snapshot.messages,
                conversation_entries=snapshot.conversation_entries,
                agent=turn_agent,
            )
        except (*RUN_ERRORS, WorkspaceError) as exc:
            return TurnFailure(
                error=exc,
                messages=partial_messages_from_error(exc),
                conversation_entries=partial_conversation_entries_from_error(exc),
                agent=turn_agent,
                rejected_prompt=(
                    starting_prompt
                    if isinstance(exc, WorkspaceUnavailable) and not execution_started
                    else None
                ),
            )
