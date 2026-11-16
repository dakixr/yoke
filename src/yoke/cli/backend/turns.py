"""In-process implementation of prompt admission and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread
from collections.abc import Iterator

from yoke.cli.backend.events import (
    BackendEvent,
    EventStream,
    EventSubscriptions,
    PUBLIC_AGENT_EVENTS,
)

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.state import capture_agent_state
from yoke.cli.config import RUN_ERRORS
from yoke.cli.backend.models import (
    PendingPrompt,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
    partial_conversation_entries_from_error,
    partial_messages_from_error,
)
from yoke.cli.runtime import (
    ActiveSession,
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
type Checkpoint = Callable[[object, list[Message], list[ConversationEntry]], None]


@dataclass(slots=True)
class TurnRunner:
    """Prepare an isolated runtime and return a typed outcome to its owner."""

    agent: object
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


class SubmittedTurn:
    """POST prompt's in-process worker, observed through GET /event and wait.

    The UI iterates events and only then reads the outcome. Agent callbacks are
    private to this worker, so an HTTP/SSE implementation can replace it.
    """

    def __init__(
        self,
        runner: TurnRunner,
        prompt: str,
        messages: list[Message],
        *,
        entries: list[ConversationEntry],
        stop_event: Event,
        user_message: Message | None,
        checkpoint: Checkpoint,
        starting_prompt: tuple[PendingPrompt, int] | None = None,
        subscriptions: EventSubscriptions | None = None,
    ) -> None:
        self._events = EventStream()
        self._outcome: TurnOutcome | None = None
        self._error: BaseException | None = None

        def execute() -> None:
            try:
                self._outcome = runner.run(
                    prompt,
                    messages,
                    entries=entries,
                    stop_event=stop_event,
                    user_message=user_message,
                    indicator=_EventPublisher(self._events, subscriptions),
                    checkpoint=checkpoint,
                    starting_prompt=starting_prompt,
                )
            except BaseException as exc:
                self._error = exc
            finally:
                self._events.close()

        self._worker = Thread(target=execute, daemon=True, name="yoke-backend-turn")
        self._worker.start()

    def __iter__(self) -> Iterator[BackendEvent]:
        return iter(self._events)

    def result(self) -> TurnOutcome:
        """POST /session/{id}/wait followed by GET /session/{id}/message."""
        self._worker.join()
        if self._error is not None:
            raise self._error
        if self._outcome is None:
            raise RuntimeError("Turn completed without an outcome.")
        return self._outcome


class _EventPublisher:
    """Agent callback adapter. It knows nothing about terminal rendering."""

    def __init__(
        self, events: EventStream, subscriptions: EventSubscriptions | None
    ) -> None:
        self._events = events
        self._subscriptions = subscriptions

    def __enter__(self) -> _EventPublisher:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    def handle_event(self, event: str, payload: dict[str, object]) -> None:
        event_type = PUBLIC_AGENT_EVENTS.get(event, event)
        self._events.publish(event_type, payload)
        if self._subscriptions is not None:
            self._subscriptions.publish(event_type, payload)
