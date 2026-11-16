"""Physical turn execution and worker-to-loop event publication."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor, Future
from threading import Lock

from yoke.agent.activity import activity_status_for_event
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import AgentContext, ConversationEntry
from yoke.agent.observability import ToolTraceStore
from yoke.http.reads.cache import SessionReadSnapshot
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.runtime.completion import retain_cancelled_worker
from yoke.http.runtime.entry_persistence import normalized_runtime_entry_count
from yoke.http.runtime.factory import http_session_usage_metric_context
from yoke.http.runtime.finalization import TurnFinalizer
from yoke.http.runtime.owned_cleanup import execute_with_cleanup
from yoke.http.runtime.persistence import TurnPersistence
from yoke.http.runtime.prompt_input import (
    validate_execution_images,
)
from yoke.http.runtime.resources import SessionRuntimeResources
from yoke.http.runtime.start import (
    RuntimeAppendPersistence,
    indexed_runtime_start,
)
from yoke.http.runtime.status import RuntimeStatus
from yoke.http.runtime.title import SessionTitleAutomation
from yoke.http.runtime.turns import TurnExecution, TurnOutcome
from yoke.http.runtime.workers import WorkerTracker
from yoke.http.runtime.workspace import (
    require_runtime_workspace,
    uses_workspace,
)
from yoke.http.services.event_broker import EventService
from yoke.session import SessionRecord, SessionStore
from yoke.http.services.client_session import ClientSession

LOGGER = logging.getLogger(__name__)


def retire_owned(
    resources: SessionRuntimeResources,
    agent: object | None,
    preparation_cleanup: list[Future[None]],
) -> Future[None]:
    """Include cleanup started by a failed agent preparation before it returned."""
    owned = resources.retire(agent)
    pending = list(preparation_cleanup)
    if owned is not None and owned not in pending:
        pending.append(owned)
    completion: Future[None] = Future()
    remaining = len(pending)
    lock = Lock()

    def finished(_done: Future[None]) -> None:
        nonlocal remaining
        with lock:
            remaining -= 1
            if remaining == 0:
                completion.set_result(None)

    if not pending:
        completion.set_result(None)
    for future in pending:
        future.add_done_callback(finished)
    return completion


class TurnExecutor:
    """Own a turn from worker submission through resource cleanup and events."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        events: EventService,
        activity: RuntimeStatus,
        resources: SessionRuntimeResources,
        persistence: TurnPersistence,
        automatic_title: SessionTitleAutomation,
        tool_traces: ToolTraceStore,
        workers: WorkerTracker,
        executor: Executor,
        active_slots: asyncio.Semaphore,
        finalizer: TurnFinalizer,
        message_index: SessionMessageIndex | None,
        indexed_runtime_seed: bool,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.events = events
        self.activity = activity
        self.resources = resources
        self.persistence = persistence
        self.automatic_title = automatic_title
        self.tool_traces = tool_traces
        self.workers = workers
        self.executor = executor
        self.active_slots = active_slots
        self.finalizer = finalizer
        self.message_index = message_index
        self.indexed_runtime_seed = indexed_runtime_seed

    def release_slot(self, execution: TurnExecution) -> None:
        if execution.slot_acquired and not execution.slot_released:
            execution.slot_released = True
            self.active_slots.release()

    def launch(self, execution: TurnExecution) -> None:
        tracker = self.workers
        tracker.admit(execution.turn_id)

        def finished(_task: asyncio.Task[None]) -> None:
            try:
                if not execution.worker_started:
                    execution.workspace_use.close()
            finally:
                tracker.release(execution.turn_id)

        execution.task = asyncio.create_task(
            self.run(execution),
            name=f"yoke-http-session-{self.session_id}-{execution.turn_id}",
        )
        execution.task.add_done_callback(finished)

    async def run(self, execution: TurnExecution) -> None:
        worker: Future[TurnOutcome] | None = None
        completion: Future[None] = Future()
        cancelled = False
        preparation_cleanup: list[Future[None]] = []
        loop = asyncio.get_running_loop()
        try:
            if execution.cold_start:
                # Preserve the admission response's head start on cold session loads.
                await asyncio.sleep(0.05)
            if self.active_slots.locked() and self.activity.active is execution:
                self.activity.activity = "Waiting for a slot"
                self.activity.publish_activity(execution)
            await self.active_slots.acquire()
            execution.slot_acquired = True
            if (
                self.activity.active is execution
                and self.activity.activity == "Waiting for a slot"
            ):
                self.activity.activity = (
                    "Loading session" if execution.cold_start else "Thinking"
                )
                self.activity.publish_activity(execution)
            if execution.retired_event.is_set():
                return
            worker = self.executor.submit(
                lambda: execute_with_cleanup(
                    lambda: self.execute(execution, loop),
                    preparation_cleanup,
                ),
            )
            execution.worker_started = True
            self.workers.retain(execution.turn_id, completion)
            outcome = await asyncio.shield(asyncio.wrap_future(worker))
            await self.finalizer.finish(execution, outcome)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # controller finalization boundary
            await self.finalizer.fail(execution, exc)
        finally:
            if worker is None:
                self.release_slot(execution)
            else:
                retain_cancelled_worker(
                    worker,
                    loop=loop,
                    retire_agent=lambda agent: retire_owned(
                        self.resources, agent, preparation_cleanup
                    ),
                    release_slot=lambda: self.release_slot(execution),
                    release_resources=execution.workspace_use.close,
                    completed=lambda: completion.set_result(None),
                )
                if not cancelled:
                    await asyncio.shield(asyncio.wrap_future(completion))

    def execute(
        self, execution: TurnExecution, loop: asyncio.AbstractEventLoop
    ) -> TurnOutcome:
        from yoke.environment import execution_environment

        with (
            self.events.privacy.scope(),
            execution_environment(
                execution.client.environment if execution.client else None
            ),
        ):
            return self._execute(execution, loop)

    def _execute(
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
                    execution.client.environment if execution.client else None,
                )
            loop.call_soon_threadsafe(self.thinking, execution)
            turn_agent = self.prepare(
                record,
                active_entries=active_entries,
                snapshot=snapshot,
                client=execution.client,
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
            require_runtime_workspace(self)

            def callback(event: str, payload: dict[str, object]) -> None:
                self.receive(loop, execution, event, payload)

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
                        execution.client.before_tool(
                            turn_agent.before_tool_call, execution.stop_event.is_set
                        )
                        if execution.client is not None
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
        client: ClientSession | None = None,
    ) -> RuntimeAgent:
        def load_active_entries() -> list[ConversationEntry]:
            assert snapshot is not None
            return snapshot.owned_active_path()

        return self.resources.prepare_turn(
            record,
            active_entries=active_entries,
            load_active_entries=load_active_entries,
            client=client,
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

    def receive(
        self,
        loop: asyncio.AbstractEventLoop,
        execution: TurnExecution,
        event: str,
        payload: dict[str, object],
    ) -> None:
        if event == "tool_execution_start":
            execution.count_tool()
        if execution.retired_event.is_set():
            return
        traced = self.events.privacy.trace_data(payload)
        traced["turn_id"] = execution.turn_id
        self.tool_traces.record_event(event, traced)
        usage_update = execution.context_usage.capture(
            event, payload, turn_id=execution.turn_id, input_id=execution.admission.id
        )
        if event == "model_end" and usage_update is not None:
            with self.persistence.lock:
                if not execution.retired_event.is_set():
                    execution.context_usage.persist_latest(
                        store=self.store, session_id=self.session_id
                    )
        loop.call_soon_threadsafe(
            self.publish, execution, event, dict(payload), usage_update
        )

    def thinking(self, execution: TurnExecution) -> None:
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        if self.activity.activity == "Loading session":
            self.activity.activity = "Thinking"
            self.activity.publish_activity(execution)

    def publish(
        self,
        execution: TurnExecution,
        event: str,
        payload: dict[str, object],
        usage_update: dict[str, object] | None,
    ) -> None:
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        active_tool_call_ids = self.activity.tool_call_ids.setdefault(
            execution.turn_id,
            set(),
        )
        call_id = payload.get("tool_call_id")
        if event == "tool_execution_start":
            if isinstance(call_id, str):
                active_tool_call_ids.add(call_id)
        elif event == "tool_execution_end" and isinstance(call_id, str):
            active_tool_call_ids.discard(call_id)
        next_activity = activity_status_for_event(
            event,
            payload,
            current=self.activity.activity or "Thinking",
        )
        if event == "tool_execution_end" and active_tool_call_ids:
            next_activity = "Running tool"
        if next_activity is not None and next_activity != self.activity.activity:
            self.activity.activity = next_activity
            self.activity.publish_activity(execution)
        if event == "model_end" and usage_update is not None:
            self.events.live(
                "session.context.updated",
                usage_update,
                session_id=self.session_id,
                location=self.activity.event_location,
            )
        event_type = _PUBLIC_AGENT_EVENTS.get(event)
        if event_type is None:
            return
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        if event == "context_usage":
            data = {**payload, **(usage_update or {})}
        else:
            data = dict(payload)
        data.pop("provider_result_projection", None)
        if event != "context_usage":
            data["turnID"] = execution.turn_id
            data["inputID"] = execution.admission.id
        self.events.live(
            event_type,
            data,
            session_id=self.session_id,
            location=self.activity.event_location,
        )


_PUBLIC_AGENT_EVENTS = {
    "model_start": "session.model.started",
    "model_end": "session.model.ended",
    "provider_retry": "session.provider.retry",
    "provider_rate_limited": "session.provider.rate_limited",
    "provider_recovered": "session.provider.recovered",
    "notice": "session.notice",
    "warning": "session.warning",
    "error": "session.error",
    "compaction_summary_start": "session.compaction.summary.started",
    "compaction_summary_end": "session.compaction.summary.ended",
    "context_compaction": "session.context.compacted",
    "assistant_message": "session.message.updated",
    "tool_execution_output_delta": "session.tool.delta",
    "tool_execution_start": "session.tool.started",
    "tool_execution_end": "session.tool.ended",
    "compaction_start": "session.compaction.started",
    "compaction_progress": "session.compaction.delta",
    "compaction_end": "session.compaction.ended",
    "context_usage": "session.context.updated",
}
