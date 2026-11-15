"""Thread-only agent preparation, execution, and checkpointing."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import AgentContext, ConversationEntry
from yoke.http.services.runtime_factory import http_session_usage_metric_context
from yoke.http.services.runtime_persistence import normalized_runtime_entry_count
from yoke.http.services.runtime_start import (
    RuntimeAppendPersistence,
    indexed_runtime_start,
)
from yoke.http.services.session_read_cache import SessionReadSnapshot
from yoke.http.services.session_runtime.execution import (
    TurnExecution,
    TurnOutcome,
)
from yoke.http.services.session_runtime.prompt_input import (
    validate_execution_images,
)
from yoke.http.services.session_runtime.workspace import (
    require_runtime_workspace,
    uses_workspace,
)
from yoke.session import SessionRecord

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime

LOGGER = logging.getLogger(__name__)


class TurnWorker:
    """Own turn worker behavior for one session."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self.runtime = runtime
        self.agent_events = runtime.agent_events
        self.automatic_title = runtime.automatic_title
        self.indexed_runtime_seed = runtime.indexed_runtime_seed
        self.message_index = runtime.message_index
        self.persistence = runtime.persistence
        self.resources = runtime.resources
        self.session_id = runtime.session_id
        self.store = runtime.store

    def execute(
        self,
        execution: TurnExecution,
        loop: asyncio.AbstractEventLoop,
    ) -> TurnOutcome:
        turn_agent: RuntimeAgent | None = None
        try:
            indexed = (
                indexed_runtime_start(
                    self.store,
                    self.message_index,
                    self.session_id,
                    has_attachments=bool(execution.admission.attachments),
                )
                if self.indexed_runtime_seed
                else None
            )
            snapshot: SessionReadSnapshot | None = None
            if indexed is None:
                snapshot = self.persistence.snapshot()
                record = snapshot.record
                active_entries = snapshot.runtime_active_path()
                if record.leaf_id is not None:
                    execution.append_persistence = RuntimeAppendPersistence(
                        runtime_entry_count=normalized_runtime_entry_count(
                            active_entries
                        ),
                        leaf_id=record.leaf_id,
                        leaf_is_instruction=bool(
                            active_entries
                            and active_entries[-1].id == record.leaf_id
                            and active_entries[-1].kind == "instruction"
                        ),
                    )
            else:
                record = indexed.record
                active_entries = indexed.entries
                execution.append_persistence = indexed.persistence
            execution.baseline_entry_ids = {entry.id for entry in active_entries}
            if execution.automatic_title and not execution.retired_event.is_set():
                loop.call_soon_threadsafe(
                    self.automatic_title.start,
                    execution.admission,
                )
            loop.call_soon_threadsafe(self.agent_events.thinking, execution)
            turn_agent = self.prepare(
                record,
                active_entries=active_entries,
                snapshot=snapshot,
            )
            validate_execution_images(execution.admission, turn_agent.provider)
            execution.context_usage.configure(
                record.context_window_tokens,
                turn_agent.context_manager.max_total_tokens,
            )
            user_message = (
                None
                if execution.admission.continuation
                else self.persistence.user_message(record, execution.admission)
            )
            # Construction can be slow enough for the directory to disappear
            # after tool binding. Reject before the model sees the input so the
            # admission remains recoverable instead of being falsely settled.
            require_runtime_workspace(self.runtime)

            def callback(event: str, payload: dict[str, object]) -> None:
                self.agent_events.receive(loop, execution, event, payload)

            with http_session_usage_metric_context(
                record,
                execution.admission.prompt,
            ):

                def checkpoint(context: AgentContext) -> None:
                    self.checkpoint(execution, context)

                execution.execution_started = True
                result = turn_agent.run(
                    execution.admission.prompt,
                    user_message=user_message,
                    append_user_message=not execution.admission.continuation,
                    on_event=callback,
                    stop_requested=execution.stop_event.is_set,
                    active_skills=record.active_skills,
                    available_skills=turn_agent.available_skills,
                    after_tool_result_appended=checkpoint,
                    before_tool_call=(
                        self.resources.client_session.before_tool(
                            turn_agent.before_tool_call, execution.stop_event.is_set
                        )
                        if self.resources.client_session is not None
                        else turn_agent.before_tool_call
                    ),
                )
            return TurnOutcome(agent=turn_agent, result=result)
        except BaseException as exc:  # worker boundary must report every failure
            partial = getattr(exc, "partial_conversation_entries", None)
            if partial is None and turn_agent is not None:
                partial = turn_agent.conversation_entries
            return TurnOutcome(
                agent=turn_agent,
                error=exc,
                partial_entries=list(partial) if partial is not None else None,
            )

    @uses_workspace
    def prepare(
        self,
        record: SessionRecord,
        *,
        active_entries: list[ConversationEntry] | None,
        snapshot: SessionReadSnapshot | None,
    ) -> RuntimeAgent:
        def load_active_entries() -> list[ConversationEntry]:
            assert snapshot is not None
            return snapshot.owned_active_path()

        return self.resources.prepare_turn(
            record,
            active_entries=active_entries,
            load_active_entries=load_active_entries,
        )

    def checkpoint(
        self,
        execution: TurnExecution,
        context: AgentContext,
    ) -> None:
        if execution.retired_event.is_set():
            return
        self.persistence.persist_turn_entries(
            execution,
            list(context.conversation_log.entries),
            input_id=execution.admission.id,
            active_skills=context.active_skills,
        )
