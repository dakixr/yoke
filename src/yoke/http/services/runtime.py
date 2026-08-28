"""One serialized HTTP-owned execution lane for a Yoke session."""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC
from datetime import datetime
import secrets
from pathlib import Path
from threading import Event
from threading import Lock
from collections.abc import Callable
from typing import Literal
from typing import TypeVar

from yoke.agent.loop import AgentResult
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.forking import promote_runtime_fork
from yoke.agent.compaction import force_compact_agent
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.multimodal import build_image_user_message
from yoke.agent.multimodal import next_image_label_index
from yoke.agent.observability import ToolTraceStore
from yoke.agent.provider_selection import ProviderSessionState
from yoke.agent.provider_selection import switch_agent_provider_model
from yoke.agent.skills import activate_skills
from yoke.agent.skills import load_skill_registry
from yoke.agent.skills.paths import default_skill_dirs
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.session_tree import SessionTree
from yoke.http.models.session import ActiveRuntimeInfo
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.redaction import redact_public_value
from yoke.http.services.runtime_factory import SessionAgentFactory
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.http.services.session_read_cache import SessionReadSnapshot
from yoke.http.services.runtime_start import RuntimeAppendPersistence
from yoke.http.services.runtime_start import indexed_runtime_start
from yoke.http.services.runtime_context_usage import RuntimeContextUsageState
from yoke.http.services.runtime_persistence import active_skill_list
from yoke.http.services.runtime_persistence import input_has_terminal_assistant
from yoke.http.services.runtime_persistence import input_is_persisted
from yoke.http.services.runtime_persistence import tag_input_entry
from yoke.session import SessionRecord
from yoke.session import SessionStore
from yoke.agent.activity import activity_status_for_event
from yoke.session.admissions import AdmissionRecord
from yoke.session.interrupt import interrupted_turn_snapshot
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import McpSessionServerPolicy

type RuntimeState = Literal["idle", "running", "stopping", "waiting_input", "error"]
T = TypeVar("T")


@dataclass(slots=True)
class TurnExecution:
    """Process-local identity for one promoted input generation."""

    turn_id: int
    admission: AdmissionRecord
    started_at: str
    stop_event: Event
    retired_event: Event
    cold_start: bool
    append_persistence: RuntimeAppendPersistence | None = None
    context_usage: RuntimeContextUsageState = dataclass_field(
        default_factory=RuntimeContextUsageState
    )
    task: asyncio.Task[None] | None = None
    slot_acquired: bool = False
    slot_released: bool = False
    worker_started: bool = False


@dataclass(slots=True)
class TurnOutcome:
    """Worker result handed back to the asyncio controller."""

    agent: object | None
    result: AgentResult | None = None
    error: BaseException | None = None
    partial_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class SessionOperation:
    """One non-prompt runtime operation serialized with session turns."""

    id: str
    kind: Literal["selection", "compaction"]
    started_at: str
    task: asyncio.Task[object] | None = None


class SessionRuntime:
    """Serialize one session while allowing retired workers to finish safely."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        pending_inputs: PendingInputService,
        events: EventService,
        agent_factory: SessionAgentFactory,
        read_cache: SessionReadCache,
        message_index: SessionMessageIndex | None,
        indexed_runtime_seed: bool,
        executor: Executor,
        active_slots: asyncio.Semaphore,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.pending_inputs = pending_inputs
        self.events = events
        self.agent_factory = agent_factory
        self.read_cache = read_cache
        self.message_index = message_index
        self.indexed_runtime_seed = indexed_runtime_seed
        index_entry = store.index_entry(session_id)
        self._event_location = index_entry.root if index_entry is not None else None
        self.executor = executor
        self.active_slots = active_slots
        self._lock = asyncio.Lock()
        self._agent_lock = Lock()
        self._persistence_lock = Lock()
        self._primary_agent: RuntimeAgent | None = None
        self._process_unsubscribe: Callable[[], None] | None = None
        self._session_enabled_tool_names: set[str] | None = None
        self._mcp_session_policy = McpSessionPolicy.empty()
        self.tool_traces = ToolTraceStore()
        self._active: TurnExecution | None = None
        self._operation: SessionOperation | None = None
        self._turn_counter = 0
        self._state: RuntimeState = "idle"
        self._last_error: str | None = None
        self._activity_status: str | None = None
        self._active_tool_call_ids: dict[int, set[str]] = {}

    async def wake(self) -> None:
        """Start eligible work or apply a pending steer at a safe control boundary."""
        async with self._lock:
            if self._operation is not None:
                return
            if self._active is not None:
                steering = self.pending_inputs.pop_next(
                    self.session_id,
                    allow_queue=False,
                )
                if steering is None:
                    return
                await self._retire_locked(reason="steering")
                self._start_locked(steering)
                return
            admission = self._recover_or_next_locked()
            if admission is not None:
                self._start_locked(admission)

    async def interrupt(self) -> tuple[bool, int | None]:
        """Retire the current generation without consuming queued work."""
        async with self._lock:
            if self._active is None:
                return False, None
            turn_id = self._active.turn_id
            await self._retire_locked(reason="user")
            return True, turn_id

    async def wait(self, timeout_seconds: float | None = None) -> ActiveRuntimeInfo:
        """Wait until the current logical generation settles."""

        async def wait_idle() -> None:
            while True:
                async with self._lock:
                    if self._active is None and self._operation is None:
                        return
                    task = (
                        self._active.task
                        if self._active is not None
                        else self._operation.task
                        if self._operation is not None
                        else None
                    )
                if task is None:
                    await asyncio.sleep(0)
                else:
                    await asyncio.shield(task)

        if timeout_seconds is None:
            await wait_idle()
        else:
            await asyncio.wait_for(wait_idle(), timeout_seconds)
        return await self.status()

    async def status(self) -> ActiveRuntimeInfo:
        """Return process-local activity without loading durable session metadata."""
        async with self._lock:
            active = self._active
            operation = self._operation
            return ActiveRuntimeInfo(
                state=self._state,
                turn_id=active.turn_id if active is not None else None,
                started_at=(
                    active.started_at
                    if active is not None
                    else operation.started_at
                    if operation is not None
                    else None
                ),
                activity=self._activity_status,
            )

    async def idle_mutation(self, mutation: Callable[[], T]) -> T:
        """Run a synchronous session mutation while prompt starts are excluded."""
        async with self._lock:
            if self._active is not None or self._operation is not None:
                from yoke.http.errors import ApiError

                raise ApiError(
                    409,
                    "session_busy",
                    "Session must be idle for this operation.",
                )
            return mutation()

    async def select_model(
        self,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        """Apply one provider/model selection while the session is idle."""
        async with self._lock:
            self._require_no_work_locked()
            operation = self._new_operation("selection")
            self._operation = operation
            self._state = "running"
            self._last_error = None
            self._activity_status = None
            self._publish_activity(None)
            task = asyncio.create_task(
                self._run_selection(
                    operation,
                    provider_name=provider_name,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                ),
                name=f"yoke-http-selection-{self.session_id}",
            )
            operation.task = task
        result = await task
        assert isinstance(result, ProviderSessionState)
        return result

    async def compact(self) -> str:
        """Schedule one manual compaction and return its operation identity."""
        async with self._lock:
            self._require_no_work_locked()
            operation = self._new_operation("compaction")
            self._operation = operation
            self._state = "running"
            self._last_error = None
            self._activity_status = "Compacting"
            self.events.durable(
                self.session_id,
                "session.compaction.started",
                {"operationID": operation.id, "reason": "manual"},
                location=self._event_location,
            )
            self._publish_activity(None)
            task = asyncio.create_task(
                self._run_compaction(operation),
                name=f"yoke-http-compaction-{self.session_id}",
            )
            operation.task = task
            return operation.id

    async def activate_skill(self, skill_name: str) -> ActiveSkill:
        """Activate one skill and append the same tree marker used by the CLI."""
        async with self._lock:
            self._require_no_work_locked()
            record = self._record()
            with self._agent_lock:
                primary = self._primary_agent
                registry = (
                    primary.skill_registry
                    if primary is not None and primary.skill_registry is not None
                    else load_skill_registry(
                        default_skill_dirs(Path(record.root or Path.cwd()))
                    )
                )
                activation = activate_skills(
                    registry=registry,
                    active_skills=(
                        primary.active_skills
                        if primary is not None
                        else record.active_skills
                    ),
                    names=[skill_name],
                )
                if activation.missing:
                    from yoke.http.errors import ApiError

                    raise ApiError(
                        404,
                        "skill_not_found",
                        f"Unknown skill: {skill_name}",
                    )
                tree = SessionTree.import_legacy(
                    record.conversation_entries,
                    record.leaf_id,
                )
                tree.append_active_skills(activation.activated_skills)
                exported = tree.export_for_persistence()
                self._persist_entries(
                    list(exported.entries),
                    input_id=None,
                    active_skills=activation.active_skills,
                )
                if primary is not None:
                    primary.active_skills = [
                        skill.model_copy(deep=True)
                        for skill in activation.active_skills
                    ]
                    active = tree.export_active_for_persistence()
                    primary.load_owned_conversation(
                        list(active.entries),
                        available_skills=primary.available_skills,
                        active_skills=activation.active_skills,
                    )
                    primary.refresh_tools(force=True)
            activated = activation.activated_skills[-1]
            self.events.durable(
                self.session_id,
                "session.skill.activated",
                {"skill": activated.name},
                location=self._event_location,
            )
            return activated.model_copy(deep=True)

    async def close(self) -> None:
        """Stop current work and release the primary runtime."""
        async with self._lock:
            if self._active is not None:
                await self._retire_locked(reason="shutdown")
            operation_task = (
                self._operation.task if self._operation is not None else None
            )
        if operation_task is not None:
            await asyncio.shield(operation_task)
        async with self._lock:
            primary = self._primary_agent
            self._primary_agent = None
            unsubscribe = self._process_unsubscribe
            self._process_unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()
        if primary is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self.executor, primary.close)

    async def _run_selection(
        self,
        operation: SessionOperation,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        await self.active_slots.acquire()
        try:
            loop = asyncio.get_running_loop()
            state = await loop.run_in_executor(
                self.executor,
                self._select_sync,
                provider_name,
                model_id,
                reasoning_effort,
            )
        except BaseException as exc:
            await self._finish_operation_error(operation, exc)
            raise
        finally:
            self.active_slots.release()
        await self._finish_operation_success(operation)
        return state

    def _select_sync(
        self,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        snapshot = self._snapshot()
        record = snapshot.record
        agent = self._ensure_primary_agent(
            record,
            load_state=True,
            active_entries=snapshot.owned_active_path(),
        )
        if not isinstance(agent, RuntimeAgent):
            raise ValueError("Model switching requires a RuntimeAgent-backed session.")
        state = switch_agent_provider_model(
            agent,
            provider_name=provider_name,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            session_id=self.session_id,
        )
        updated = self.store.set_selection(
            self.session_id,
            provider_name=state.provider_name,
            model_id=state.model_id,
            reasoning_effort=state.reasoning_effort,
            context_window_tokens=state.context_window_tokens,
            existing_record=record,
        )
        self.events.durable(
            self.session_id,
            "session.selection.changed",
            {
                "provider": updated.provider_name,
                "model": updated.model_id,
                "reasoningEffort": updated.reasoning_effort,
            },
            location=updated.root,
        )
        return state

    async def _run_compaction(self, operation: SessionOperation) -> object:
        await self.active_slots.acquire()
        try:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(self.executor, self._compact_sync)
        except BaseException as exc:
            await self._finish_operation_error(operation, exc)
            raise
        finally:
            self.active_slots.release()
        await self._finish_operation_success(operation)
        return payload

    def _compact_sync(self) -> dict[str, object]:
        snapshot = self._snapshot()
        record = snapshot.record
        agent = self._ensure_primary_agent(
            record,
            load_state=True,
            active_entries=snapshot.owned_active_path(),
        )
        if not isinstance(agent, RuntimeAgent):
            raise ValueError("Compaction requires a RuntimeAgent-backed session.")
        compacted = force_compact_agent(
            agent,
            agent.messages,
            conversation_entries=agent.conversation_entries,
        )
        payload: dict[str, object]
        if compacted is None:
            payload = {"compacted": False}
        else:
            self._persist_entries(
                compacted.conversation_entries,
                input_id=None,
                active_skills=agent.active_skills,
            )
            payload = {
                "compacted": True,
                "summarizedMessages": len(compacted.preparation.messages_to_summarize),
                "keptMessages": len(compacted.preparation.kept_messages),
                "messageCount": len(compacted.messages),
                "inputTokens": compacted.preparation.estimate.input_tokens,
                "compactedInputTokens": compacted.compacted_estimate.input_tokens,
            }
        self.events.durable(
            self.session_id,
            "session.compaction.ended",
            payload,
            location=self._event_location,
        )
        return payload

    async def _finish_operation_success(self, operation: SessionOperation) -> None:
        async with self._lock:
            if self._operation is not operation:
                return
            self._operation = None
            self._state = "idle"
            self._last_error = None
            self._activity_status = None
            self._publish_activity(None)
            next_admission = self._recover_or_next_locked()
            if next_admission is not None:
                self._start_locked(next_admission)

    async def _finish_operation_error(
        self,
        operation: SessionOperation,
        error: BaseException,
    ) -> None:
        async with self._lock:
            if self._operation is not operation:
                return
            self._operation = None
            self._state = "error"
            self._last_error = str(error)
            self._activity_status = None
            self.events.durable(
                self.session_id,
                f"session.{operation.kind}.failed",
                {"operationID": operation.id, "error": self._last_error},
                location=self._event_location,
            )
            self._publish_activity(None)

    def _new_operation(
        self,
        kind: Literal["selection", "compaction"],
    ) -> SessionOperation:
        return SessionOperation(
            id=f"op_{secrets.token_hex(12)}",
            kind=kind,
            started_at=datetime.now(UTC).isoformat(),
        )

    def _require_no_work_locked(self) -> None:
        if self._active is not None or self._operation is not None:
            from yoke.http.errors import ApiError

            raise ApiError(
                409,
                "session_busy",
                "Session must be idle for this operation.",
            )

    def process_manager(self) -> CommandProcessManager | None:
        """Return the live command manager when this runtime has built an agent."""
        with self._agent_lock:
            if self._primary_agent is None:
                return None
            return self._primary_agent.command_process_manager

    def tool_trace_store(self) -> ToolTraceStore:
        """Return this runtime's shared live tool trace store."""
        return self.tool_traces

    def latest_turn_id(self) -> int:
        """Return the newest process-local turn id without touching persistence."""
        return self._turn_counter

    def _record(self) -> SessionRecord:
        """Return the shared signature-consistent record snapshot for this session."""
        return self._snapshot().record

    def _record_for_execution(self, execution: TurnExecution) -> SessionRecord:
        """Return turn metadata without forcing a full parse for indexed starts."""
        if execution.append_persistence is None:
            return self._record()
        record = self.store.summary_record(self.session_id)
        if record is None:
            raise FileNotFoundError(self.session_id)
        if record.root is not None:
            self._event_location = record.root
        return record

    def _snapshot(self) -> SessionReadSnapshot:
        """Return the shared parsed snapshot and keep event location current."""
        snapshot = self.read_cache.get(self.session_id)
        record = snapshot.record
        if record.root is not None:
            self._event_location = record.root
        return snapshot

    def session_enabled_tool_names(self) -> set[str] | None:
        """Return the process-local tool allowlist without loading an agent."""
        with self._agent_lock:
            return (
                set(self._session_enabled_tool_names)
                if self._session_enabled_tool_names is not None
                else None
            )

    def mcp_session_policy(self) -> McpSessionPolicy:
        """Return a defensive process-local MCP policy snapshot."""
        with self._agent_lock:
            return McpSessionPolicy(servers=dict(self._mcp_session_policy.servers))

    async def set_mcp_policy(
        self,
        server_name: str,
        *,
        enabled: bool | None,
        enabled_tools: tuple[str, ...] | None,
        disabled_tools: tuple[str, ...] | None,
        update_enabled_tools: bool = False,
        update_disabled_tools: bool = False,
    ) -> None:
        """Apply session-only MCP server and tool policy."""
        async with self._lock:
            self._require_no_work_locked()
            with self._agent_lock:
                existing = self._mcp_session_policy.servers.get(server_name)
                self._mcp_session_policy.servers[server_name] = McpSessionServerPolicy(
                    enabled=(
                        enabled
                        if enabled is not None
                        else existing.enabled
                        if existing
                        else None
                    ),
                    enabled_tools=(
                        enabled_tools
                        if update_enabled_tools
                        else existing.enabled_tools
                        if existing
                        else None
                    ),
                    disabled_tools=(
                        disabled_tools
                        if update_disabled_tools
                        else existing.disabled_tools
                        if existing
                        else None
                    ),
                )
                primary = self._primary_agent
                if primary is not None:
                    object.__setattr__(
                        primary.provider,
                        "_yoke_mcp_session_policy",
                        self._mcp_session_policy,
                    )
                    primary.refresh_tools(force=True)
                    if primary._context is not None:
                        primary._sync_context_instructions(primary._context)
            self.events.live(
                "session.mcp.updated",
                {"server": server_name},
                session_id=self.session_id,
                location=self._event_location,
            )

    async def set_tools(
        self,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        """Apply a session-only tool allowlist without changing config files."""
        async with self._lock:
            self._require_no_work_locked()
            unknown = (enabled | disabled) - discovered_names
            if unknown:
                from yoke.http.errors import ApiError

                raise ApiError(
                    400,
                    "unknown_tool",
                    f"Unknown tools: {', '.join(sorted(unknown))}.",
                )
            base = (
                set(self._session_enabled_tool_names)
                if self._session_enabled_tool_names is not None
                else set(default_enabled_names)
            )
            next_enabled = (base | enabled) - disabled
            self._session_enabled_tool_names = set(next_enabled)
            with self._agent_lock:
                primary = self._primary_agent
                if primary is not None:
                    hidden_names = set(primary.tools) - discovered_names
                    primary.set_session_enabled_tools(next_enabled | hidden_names)
                    primary._install_session_filtered_tool_system_messages()
                    if primary._context is not None:
                        primary._sync_context_instructions(primary._context)
            self.events.live(
                "session.tool.config.changed",
                {"enabled": sorted(next_enabled)},
                session_id=self.session_id,
                location=self._event_location,
            )
            return set(next_enabled)

    def _recover_or_next_locked(self) -> AdmissionRecord | None:
        while True:
            promoted = self.pending_inputs.unsettled_promoted(self.session_id)
            if promoted is None:
                return self.pending_inputs.pop_next(self.session_id, allow_queue=True)
            snapshot = self._snapshot()
            record = snapshot.record
            if not input_is_persisted(record, promoted.id):
                return promoted
            if not input_has_terminal_assistant(
                snapshot.active_path_entries,
                promoted.id,
            ):
                self._persist_interrupted_checkpoint(promoted)
            self.pending_inputs.settle(
                self.session_id,
                promoted.id,
                outcome="recovered",
            )

    def _start_locked(self, admission: AdmissionRecord) -> None:
        self._turn_counter += 1
        cold_start = self._primary_agent is None and not self.read_cache.is_current(
            self.session_id
        )
        execution = TurnExecution(
            turn_id=self._turn_counter,
            admission=admission,
            started_at=datetime.now(UTC).isoformat(),
            stop_event=Event(),
            retired_event=Event(),
            cold_start=cold_start,
        )
        self._active = execution
        self._state = "running"
        self._last_error = None
        self._activity_status = "Loading session" if cold_start else "Thinking"
        self._active_tool_call_ids = {execution.turn_id: set()}
        self._publish_activity(execution)
        execution.task = asyncio.create_task(
            self._run_execution(execution),
            name=f"yoke-http-session-{self.session_id}-{execution.turn_id}",
        )

    async def _retire_locked(self, *, reason: str) -> None:
        execution = self._active
        if execution is None:
            return
        self._state = "stopping"
        execution.stop_event.set()
        execution.retired_event.set()
        self.tool_traces.retire_turn(execution.turn_id)
        self._persist_interrupted_checkpoint(execution.admission)
        self.pending_inputs.settle(
            self.session_id,
            execution.admission.id,
            outcome="stopped",
        )
        self.events.durable(
            self.session_id,
            "session.interrupted",
            {
                "inputID": execution.admission.id,
                "turnID": execution.turn_id,
                "reason": reason,
            },
            location=self._event_location,
        )
        self._release_slot(execution)
        if execution.task is not None and not execution.worker_started:
            execution.task.cancel()
        self._active = None
        self._state = "idle"
        self._activity_status = None
        self._active_tool_call_ids.pop(execution.turn_id, None)
        self._publish_activity(None)

    async def _run_execution(self, execution: TurnExecution) -> None:
        try:
            if execution.cold_start:
                # Let the prompt-admission response flush before a cold large-session
                # parse starts competing for CPU in the worker pool.
                await asyncio.sleep(0.05)
            await self.active_slots.acquire()
            execution.slot_acquired = True
            if execution.retired_event.is_set():
                self._release_slot(execution)
                return
            execution.worker_started = True
            loop = asyncio.get_running_loop()
            outcome = await loop.run_in_executor(
                self.executor,
                self._execute_sync,
                execution,
            )
            await self._finish_execution(execution, outcome)
        except asyncio.CancelledError:
            self._release_slot(execution)
            raise

    def _execute_sync(self, execution: TurnExecution) -> TurnOutcome:
        turn_agent: object | None = None
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
                snapshot = self._snapshot()
                record = snapshot.record
                active_entries = snapshot.runtime_active_path()
                if record.leaf_id is not None:
                    execution.append_persistence = RuntimeAppendPersistence(
                        runtime_entry_count=len(active_entries),
                        leaf_id=record.leaf_id,
                    )
            else:
                record = indexed.record
                active_entries = indexed.entries
                execution.append_persistence = indexed.persistence
                if record.root is not None:
                    self._event_location = record.root
            if self._activity_status == "Loading session":
                self._activity_status = "Thinking"
                self._publish_activity(execution)
            turn_agent = self._prepare_turn_agent(
                record,
                active_entries=active_entries,
                snapshot=snapshot,
            )
            execution.context_usage.configure(
                record.context_window_tokens,
                (
                    turn_agent.context_manager.max_total_tokens
                    if isinstance(turn_agent, RuntimeAgent)
                    else None
                ),
            )
            user_message = self._user_message_for_admission(record, execution.admission)

            def callback(event: str, payload: dict[str, object]) -> None:
                self._on_agent_event(execution, event, payload)

            if isinstance(turn_agent, RuntimeAgent):

                def checkpoint(context: AgentContext) -> None:
                    self._checkpoint(execution, turn_agent, context)

                result = turn_agent.run(
                    execution.admission.prompt,
                    user_message=user_message,
                    on_event=callback,
                    stop_requested=execution.stop_event.is_set,
                    active_skills=record.active_skills,
                    available_skills=turn_agent.available_skills,
                    after_tool_result_appended=checkpoint,
                )
            else:
                run = getattr(turn_agent, "run")
                kwargs: dict[str, object] = {
                    "on_event": callback,
                    "stop_requested": execution.stop_event.is_set,
                }
                if getattr(turn_agent, "supports_user_message", False):
                    kwargs["user_message"] = user_message
                result = run(execution.admission.prompt, **kwargs)
            return TurnOutcome(agent=turn_agent, result=result)
        except BaseException as exc:  # worker boundary must report every failure
            partial = getattr(exc, "partial_conversation_entries", None)
            if partial is None and isinstance(turn_agent, RuntimeAgent):
                partial = turn_agent.conversation_entries
            return TurnOutcome(
                agent=turn_agent,
                error=exc,
                partial_entries=list(partial) if partial is not None else None,
            )

    async def _finish_execution(
        self,
        execution: TurnExecution,
        outcome: TurnOutcome,
    ) -> None:
        async with self._lock:
            self._release_slot(execution)
            if execution.retired_event.is_set() or self._active is not execution:
                self._close_turn_agent_later(outcome.agent)
                return
            record = self._record_for_execution(execution)
            if outcome.result is not None:
                entries = outcome.result.conversation_entries or []
                if entries:
                    self._persist_turn_entries(
                        execution,
                        entries,
                        input_id=execution.admission.id,
                        active_skills=(
                            outcome.agent.active_skills
                            if isinstance(outcome.agent, RuntimeAgent)
                            else record.active_skills
                        ),
                    )
                if outcome.result.status == "completed":
                    self._promote_runtime_agent(outcome.agent)
                    settlement = "completed"
                else:
                    settlement = "stopped"
                self.pending_inputs.settle(
                    self.session_id,
                    execution.admission.id,
                    outcome=settlement,
                )
                record = self._record_for_execution(execution)
                self.events.durable(
                    self.session_id,
                    "session.message.updated",
                    {
                        "inputID": execution.admission.id,
                        "turnID": execution.turn_id,
                        "status": outcome.result.status,
                        "leafID": record.leaf_id,
                    },
                    location=record.root,
                )
            else:
                if outcome.partial_entries:
                    self._persist_turn_entries(
                        execution,
                        outcome.partial_entries,
                        input_id=execution.admission.id,
                    )
                    record = self._record_for_execution(execution)
                self.pending_inputs.settle(
                    self.session_id,
                    execution.admission.id,
                    outcome="failed",
                )
                self._last_error = str(outcome.error or "Agent execution failed.")
                self.events.durable(
                    self.session_id,
                    "session.runtime.failed",
                    {
                        "inputID": execution.admission.id,
                        "turnID": execution.turn_id,
                        "error": self._last_error,
                    },
                    location=record.root,
                )
            record = execution.context_usage.persist(
                store=self.store,
                events=self.events,
                session_id=self.session_id,
                record=record,
            )
            self._close_turn_agent_later(outcome.agent)
            self._active = None
            self._state = "error" if outcome.error is not None else "idle"
            self._activity_status = None
            self._active_tool_call_ids.pop(execution.turn_id, None)
            self._publish_activity(None)
            next_admission = self._recover_or_next_locked()
            if next_admission is not None:
                self._start_locked(next_admission)

    def _prepare_turn_agent(
        self,
        record: SessionRecord,
        *,
        active_entries: list[ConversationEntry] | None,
        snapshot: SessionReadSnapshot | None,
    ) -> object:
        primary_or_candidate = self._ensure_primary_agent(record, load_state=False)
        if not isinstance(primary_or_candidate, RuntimeAgent):
            return primary_or_candidate
        with self._agent_lock:
            primary = self._primary_agent
            assert primary is not None
            turn_agent = primary.fork(isolate_provider=True, include_state=False)
            if active_entries is None:
                assert snapshot is not None
                active_entries = snapshot.owned_active_path()
            turn_agent.load_owned_conversation(
                active_entries,
                available_skills=primary.available_skills,
                active_skills=primary.active_skills,
            )
            return turn_agent

    def _ensure_primary_agent(
        self,
        record: SessionRecord,
        *,
        load_state: bool,
        active_entries: list[ConversationEntry] | None = None,
    ) -> object:
        with self._agent_lock:
            if self._primary_agent is None:
                candidate = self.agent_factory(record)
                if not isinstance(candidate, RuntimeAgent):
                    return candidate
                self._primary_agent = candidate
                object.__setattr__(
                    candidate.provider,
                    "_yoke_mcp_session_policy",
                    self._mcp_session_policy,
                )
                candidate.refresh_tools(force=True)
                self._process_unsubscribe = candidate.command_process_manager.subscribe(
                    self._on_process_change
                )
                if self._session_enabled_tool_names is not None:
                    candidate.set_session_enabled_tools(
                        self._session_enabled_tool_names
                    )
            primary = self._primary_agent
            assert primary is not None
            if load_state:
                if active_entries is None:
                    active_entries = self._snapshot().owned_active_path()
                primary.load_owned_conversation(
                    active_entries,
                    available_skills=primary.available_skills,
                    active_skills=record.active_skills,
                )
            return primary

    def _promote_runtime_agent(self, turn_agent: object | None) -> None:
        if not isinstance(turn_agent, RuntimeAgent):
            return
        with self._agent_lock:
            primary = self._primary_agent
            if primary is not None and turn_agent is not primary:
                promote_runtime_fork(primary, turn_agent)

    def _checkpoint(
        self,
        execution: TurnExecution,
        turn_agent: RuntimeAgent,
        context: AgentContext,
    ) -> None:
        if execution.retired_event.is_set():
            return
        self._persist_turn_entries(
            execution,
            list(context.conversation_log.entries),
            input_id=execution.admission.id,
            active_skills=context.active_skills,
        )

    def _persist_interrupted_checkpoint(self, admission: AdmissionRecord) -> None:
        with self._persistence_lock:
            snapshot = self._snapshot()
            record = snapshot.record
            active_entries = snapshot.owned_active_path()
            user_message = (
                None
                if input_is_persisted(record, admission.id)
                else self._user_message_for_admission(record, admission)
            )
            _, entries = interrupted_turn_snapshot(
                messages=(),
                entries=active_entries,
                user_message=user_message,
                leaf_id=record.leaf_id,
            )
            self._save_entries_locked(entries, input_id=admission.id)

    def _user_message_for_admission(
        self,
        record: SessionRecord,
        admission: AdmissionRecord,
    ) -> Message:
        if not admission.attachments:
            return Message.user(admission.prompt)
        paths = [
            self.pending_inputs.uploads.resolve(
                attachment.uri,
                session_id=self.session_id,
                name=attachment.name,
                mime=attachment.mime,
            )
            for attachment in admission.attachments
        ]
        return build_image_user_message(
            admission.prompt,
            image_paths=paths,
            start_index=next_image_label_index(record.messages),
            embed_local_images=False,
        )

    def _persist_entries(
        self,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        with self._persistence_lock:
            self._save_entries_locked(
                entries,
                input_id=input_id,
                active_skills=active_skills,
            )

    def _persist_turn_entries(
        self,
        execution: TurnExecution,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        persistence = execution.append_persistence
        if persistence is None:
            self._persist_entries(
                entries,
                input_id=input_id,
                active_skills=active_skills,
            )
            return
        skills = active_skill_list(active_skills)
        with self._persistence_lock:
            persistence.append(
                self.store,
                self.session_id,
                entries,
                input_id=input_id,
                active_skills=skills,
            )

    def _save_entries_locked(
        self,
        entries: list[ConversationEntry],
        *,
        input_id: str | None,
        active_skills: object | None = None,
    ) -> None:
        current = self._record()
        copied = [entry.model_copy(deep=True) for entry in entries]
        if input_id is not None:
            tag_input_entry(copied, input_id)
        leaf_id = copied[-1].id if copied else current.leaf_id
        resolved_skills = active_skill_list(active_skills)
        skills = current.active_skills if resolved_skills is None else resolved_skills
        self.store.save(
            self.session_id,
            [],
            conversation_entries=copied,
            leaf_id=leaf_id,
            active_skills=skills,
            skill_dirs=current.skill_dirs,
            root=current.root,
            title=current.title,
            provider_name=current.provider_name,
            model_id=current.model_id,
            reasoning_effort=current.reasoning_effort,
            context_window_tokens=current.context_window_tokens,
            existing_record=current,
        )

    def _on_agent_event(
        self,
        execution: TurnExecution,
        event: str,
        payload: dict[str, object],
    ) -> None:
        if execution.retired_event.is_set():
            return
        traced_payload = dict(payload)
        traced_payload["turn_id"] = execution.turn_id
        self.tool_traces.record_event(event, traced_payload)
        if execution.retired_event.is_set() or self._active is not execution:
            return
        active_tool_call_ids = self._active_tool_call_ids.setdefault(
            execution.turn_id,
            set(),
        )
        call_id = payload.get("tool_call_id")
        if event == "tool_execution_start" and isinstance(call_id, str):
            active_tool_call_ids.add(call_id)
        elif event == "tool_execution_end" and isinstance(call_id, str):
            active_tool_call_ids.discard(call_id)
        next_activity = activity_status_for_event(
            event,
            payload,
            current=self._activity_status or "Thinking",
        )
        if event == "tool_execution_end" and active_tool_call_ids:
            next_activity = "Running tool"
        if next_activity is not None and next_activity != self._activity_status:
            self._activity_status = next_activity
            self._publish_activity(execution)
        usage_update = execution.context_usage.capture(
            event,
            payload,
            turn_id=execution.turn_id,
            input_id=execution.admission.id,
        )
        if event == "model_end" and usage_update is not None:
            self.events.live(
                "session.context.updated",
                usage_update,
                session_id=self.session_id,
                location=self._event_location,
            )
        event_type = _PUBLIC_AGENT_EVENTS.get(event)
        if event_type is None:
            return
        if execution.retired_event.is_set() or self._active is not execution:
            return
        if event == "context_usage":
            data = usage_update or {}
        else:
            redacted = redact_public_value(payload)
            data = dict(redacted) if isinstance(redacted, dict) else {"value": redacted}
        if event != "context_usage":
            data["turnID"] = execution.turn_id
            data["inputID"] = execution.admission.id
        self.events.live(
            event_type,
            data,
            session_id=self.session_id,
            location=self._event_location,
        )

    def _publish_activity(self, execution: TurnExecution | None) -> None:
        if execution is not None and (
            execution.retired_event.is_set() or self._active is not execution
        ):
            return
        if execution is not None and (
            execution.retired_event.is_set() or self._active is not execution
        ):
            return
        self.events.live(
            "session.active.changed",
            {
                "state": self._state,
                "turnID": execution.turn_id if execution is not None else None,
                "startedAt": execution.started_at if execution is not None else None,
                "error": self._last_error,
                "activity": self._activity_status,
            },
            session_id=self.session_id,
            location=self._event_location,
        )

    def _on_process_change(self) -> None:
        self.events.live(
            "session.process.updated",
            {},
            session_id=self.session_id,
            location=self._event_location,
        )

    def _release_slot(self, execution: TurnExecution) -> None:
        if execution.slot_acquired and not execution.slot_released:
            execution.slot_released = True
            self.active_slots.release()

    def _close_turn_agent_later(self, turn_agent: object | None) -> None:
        if not isinstance(turn_agent, RuntimeAgent):
            return
        primary = self._primary_agent
        provider = getattr(turn_agent, "provider", None)

        def close() -> None:
            turn_agent.close()
            close_provider = getattr(provider, "close", None)
            if callable(close_provider) and provider is not getattr(
                primary, "provider", None
            ):
                close_provider()

        asyncio.get_running_loop().run_in_executor(self.executor, close)


_PUBLIC_AGENT_EVENTS = {
    "assistant_message": "session.message.updated",
    "tool_execution_start": "session.tool.started",
    "tool_execution_end": "session.tool.ended",
    "compaction_start": "session.compaction.started",
    "compaction_progress": "session.compaction.delta",
    "compaction_end": "session.compaction.ended",
    "context_usage": "session.context.updated",
}
