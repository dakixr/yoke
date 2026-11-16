"""Idle configuration, model selection, compaction, and workspace relocation."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

from yoke.agent.compaction import force_compact_agent
from yoke.environment import execution_environment
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.provider_selection import (
    ProviderSessionState,
    switch_agent_provider_model,
)
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills import activate_skills, load_skill_registry
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.paths import default_skill_dirs
from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.client_session import ClientSessionRequest
from yoke.http.runtime.admission import TurnAdmission
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.runtime.start import indexed_runtime_start
from yoke.http.runtime.factory import http_session_usage_metric_context
from yoke.http.runtime.persistence import TurnPersistence, run_blocking
from yoke.http.runtime.resources import SessionRuntimeResources
from yoke.http.runtime.status import RuntimeStatus
from yoke.http.runtime.title import SessionTitleAutomation
from yoke.http.runtime.turns import (
    ReservedInput,
    SessionOperation,
)
from yoke.http.runtime.workers import WorkerTracker
from yoke.http.runtime.workspace import (
    require_runtime_workspace,
    uses_workspace,
)
from yoke.http.services.client_session import ClientSession
from yoke.http.services.event_broker import EventService
from yoke.http.services.human_input_service import HumanInputService
from yoke.session import SessionRecord, SessionStore
from yoke.session.workspace import (
    WorkspaceBusy,
    WorkspaceConflict,
    relocate_session_workspace,
    require_workspace,
    workspace_lease,
)

LOGGER = logging.getLogger(__name__)


class SessionOperations:
    """Serialize idle operations against turn admission using explicit resources."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        events: EventService,
        activity: RuntimeStatus,
        resources: SessionRuntimeResources,
        persistence: TurnPersistence,
        inputs: TurnAdmission,
        gate: asyncio.Lock,
        active_slots: asyncio.Semaphore,
        executor: Executor,
        storage_executor: Executor,
        start: Callable[[ReservedInput], None],
        automatic_title: SessionTitleAutomation,
        workers: WorkerTracker,
        message_index: SessionMessageIndex | None = None,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.events = events
        self.activity = activity
        self.resources = resources
        self.persistence = persistence
        self.inputs = inputs
        self.gate = gate
        self.active_slots = active_slots
        self.executor = executor
        self.storage_executor = storage_executor
        self.start = start
        self.automatic_title = automatic_title
        self.workers = workers
        self.message_index = message_index

    async def idle_mutation[T](self, mutation: Callable[[], T]) -> T:
        """Exclude prompt starts and workspace relocation during a storage mutation."""

        def mutate() -> T:
            with workspace_lease(self.store, self.session_id):
                self.resources.synchronize_workspace()
                return mutation()

        async with self.gate:
            self.activity.require_idle()
            return await run_blocking(self.storage_executor, mutate)

    async def select_model(
        self,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        """Apply one provider/model selection while the session is idle."""
        async with self.gate:
            self.activity.require_idle()
            await run_blocking(self.storage_executor, require_runtime_workspace, self)
            operation = self._new_operation("selection")
            self.activity.operation = operation
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
        async with self.gate:
            self.activity.require_idle()
            await run_blocking(self.storage_executor, require_runtime_workspace, self)
            operation = self._new_operation("compaction")
            self.activity.operation = operation
            self.activity.state = "running"
            self.activity.last_error = None
            self.activity.activity = "Compacting"
            self.events.durable(
                self.session_id,
                "session.compaction.started",
                {"operationID": operation.id, "reason": "manual"},
                location=self.activity.event_location,
            )
            self.activity.publish_activity(None)
            task = asyncio.create_task(
                self._run_compaction(operation),
                name=f"yoke-http-compaction-{self.session_id}",
            )
            operation.task = task
            return operation.id

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

    @uses_workspace
    def _select_sync(
        self,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        indexed = indexed_runtime_start(
            self.store, self.message_index, self.session_id, has_attachments=False
        )
        if indexed is not None:
            record = indexed.record
            agent = self.resources.ensure_primary(
                record, load_state=True, active_entries=indexed.entries
            )
        else:
            summary = self.store.summary_record(self.session_id)
            if summary is not None and summary.leaf_id is None:
                record = summary
                agent = self.resources.ensure_primary(
                    record, load_state=True, active_entries=[]
                )
            else:
                record, agent = self._primary()
        before = agent.conversation_marker
        try:
            with (
                http_session_usage_metric_context(record),
                self.events.privacy.scope(),
                execution_environment(self.resources.environment(record)),
            ):
                state = switch_agent_provider_model(
                    agent,
                    provider_name=provider_name,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                    session_id=self.session_id,
                    retire_previous=self.resources.retire_provider,
                )
        finally:
            self.resources.register_provider(agent.provider)
        if agent.conversation_marker != before:
            self.persistence.persist_entries(
                agent.conversation_entries,
                input_id=None,
                active_skills=agent.active_skills,
            )
        try:
            updated = self.store.set_selection(
                self.session_id,
                provider_name=state.provider_name,
                model_id=state.model_id,
                reasoning_effort=state.reasoning_effort,
                context_window_tokens=state.context_window_tokens,
                existing_record=record,
            )
        except (OSError, sqlite3.Error) as exc:
            raise ApiError(
                507,
                "selection_metadata_not_saved",
                f"Model switched, but session metadata was not saved: {exc}",
                {
                    "provider": state.provider_name,
                    "model": state.model_id,
                    "reasoningEffort": state.reasoning_effort,
                    "contextWindowTokens": state.context_window_tokens,
                },
            ) from exc
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
        except Exception as exc:  # accepted operations settle into runtime status
            await self._finish_operation_error(operation, exc)
            return {"compacted": False}
        finally:
            self.active_slots.release()
        await self._finish_operation_success(operation)
        return payload

    @uses_workspace
    def _compact_sync(self) -> dict[str, object]:
        record, agent = self._primary()
        with (
            http_session_usage_metric_context(record),
            execution_environment(self.resources.environment(record)),
        ):
            compacted = force_compact_agent(
                agent,
                agent.messages,
                conversation_entries=agent.conversation_entries,
            )
        payload: dict[str, object]
        if compacted is None:
            payload = {"compacted": False}
        else:
            self.persistence.persist_entries(
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
            location=self.activity.event_location,
        )
        return payload

    async def _finish_operation_success(self, operation: SessionOperation) -> None:
        async with self.gate:
            if self.activity.operation is not operation:
                return
            self.activity.operation = None
            self.activity.last_error = None
            self.activity.activity = None
            next_admission = await self.inputs.reserve_input()
            if next_admission is not None:
                self.start(next_admission)
            else:
                self.activity.state = "error" if self.activity.last_error else "idle"
                self.activity.publish_activity(None)

    async def _finish_operation_error(
        self,
        operation: SessionOperation,
        error: BaseException,
    ) -> None:
        async with self.gate:
            if self.activity.operation is not operation:
                return
            self.activity.operation = None
            self.activity.state = "error"
            self.activity.last_error = str(error)
            self.activity.activity = None
            self.activity.best_effort_durable(
                f"session.{operation.kind}.failed",
                {"operationID": operation.id, "error": self.activity.last_error},
            )
            self.activity.best_effort_publish_activity(None)
            if operation.kind == "compaction":
                next_admission = await self.inputs.reserve_input()
                if next_admission is not None:
                    self.start(next_admission)

    def _new_operation(
        self,
        kind: Literal["selection", "compaction"],
    ) -> SessionOperation:
        return SessionOperation(
            id=f"op_{secrets.token_hex(12)}",
            kind=kind,
            started_at=datetime.now(UTC).isoformat(),
        )

    def _primary(self) -> tuple[SessionRecord, RuntimeAgent]:
        snapshot = self.persistence.snapshot()
        agent = self.resources.ensure_primary(
            snapshot.record,
            load_state=True,
            active_entries=snapshot.owned_active_path(),
        )
        if not isinstance(agent, RuntimeAgent):
            raise ValueError("This operation requires a RuntimeAgent-backed session.")
        return snapshot.record, agent

    async def activate_skill(self, skill_name: str) -> ActiveSkill:
        return await self.idle_mutation(partial(self._activate_skill_sync, skill_name))

    async def set_tools(
        self,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        return await self.idle_mutation(
            partial(
                self._set_tools_sync,
                discovered_names=discovered_names,
                default_enabled_names=default_enabled_names,
                enabled=enabled,
                disabled=disabled,
            )
        )

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
        def apply() -> None:
            self.resources.set_mcp_policy(
                server_name,
                enabled=enabled,
                enabled_tools=enabled_tools,
                disabled_tools=disabled_tools,
                update_enabled_tools=update_enabled_tools,
                update_disabled_tools=update_disabled_tools,
            )
            self.events.live(
                "session.mcp.updated",
                {"server": server_name},
                session_id=self.session_id,
                location=self.activity.event_location,
            )

        await self.idle_mutation(apply)

    def _activate_skill_sync(self, skill_name: str) -> ActiveSkill:
        """Activate one skill and append the same tree marker used by the CLI."""
        record = self.persistence.record()
        with self.resources.lock:
            primary = self.resources.primary_locked()
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
            self.persistence.persist_entries(
                list(exported.entries),
                input_id=None,
                active_skills=activation.active_skills,
            )
            if primary is not None:
                primary.active_skills = [
                    skill.model_copy(deep=True) for skill in activation.active_skills
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
            location=self.activity.event_location,
        )
        return activated.model_copy(deep=True)

    def _set_tools_sync(
        self,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        """Apply a session-only tool allowlist without changing config files."""
        unknown = (enabled | disabled) - discovered_names
        if unknown:
            raise ApiError(
                400,
                "unknown_tool",
                f"Unknown tools: {', '.join(sorted(unknown))}.",
            )
        next_enabled = self.resources.set_tools(
            discovered_names=discovered_names,
            default_enabled_names=default_enabled_names,
            enabled=enabled,
            disabled=disabled,
        )
        self.events.live(
            "session.tool.config.changed",
            {"enabled": sorted(next_enabled)},
            session_id=self.session_id,
            location=self.activity.event_location,
        )
        return set(next_enabled)

    async def relocate_workspace(
        self, directory: str, *, expected_root: str | None
    ) -> SessionRecord:
        target = str(require_workspace(directory))
        record = await run_blocking(
            self.storage_executor, self.store.summary_record, self.session_id
        )
        if record is None:
            raise session_not_found()
        if record.root == target:
            return record
        if expected_root is not None and expected_root != record.root:
            raise WorkspaceConflict(self.session_id, record.root)
        if await run_blocking(self.storage_executor, self.resources.has_live_work):
            raise WorkspaceBusy(self.session_id)

        def relocate() -> SessionRecord:
            with self.persistence.lock:
                return relocate_session_workspace(
                    self.store,
                    self.session_id,
                    target,
                    expected_root=expected_root
                    if expected_root is not None
                    else record.root,
                )

        updated = await run_blocking(self.storage_executor, relocate)
        self.automatic_title.cancel()
        await self.resources.reset_workspace(updated)
        self.activity.event_location = updated.root
        self.activity.state = "idle"
        self.activity.last_error = None
        self.activity.activity = None
        self.events.durable(
            self.session_id,
            "session.workspace.relocated",
            {
                "sessionID": self.session_id,
                "previousDirectory": record.root,
                "directory": updated.root,
            },
            location=updated.root,
        )
        self.activity.best_effort_publish_activity(None)
        return updated

    async def configure_client(
        self,
        config: ClientSessionRequest,
        human_input: HumanInputService,
    ) -> dict[str, object]:
        def configure() -> dict[str, object]:
            record = self.store.summary_record(self.session_id)
            if record is None:
                raise session_not_found()
            client = ClientSession(
                self.session_id,
                config,
                human_input,
                self.events,
                Path(record.root or Path.cwd()),
            )
            self.events.privacy.register(client.environment.values)
            self.resources.configure_client(client)
            return {
                "mode": config.mode,
                "additionalDirectories": config.additional_directories,
                "mcpServers": [server.name for server in config.mcp_servers],
            }

        async with self.gate:
            return await run_blocking(self.storage_executor, configure)

    async def detach_client(self, owner: str) -> None:
        async with self.gate:
            self.resources.configuration.clients.detach(owner)
