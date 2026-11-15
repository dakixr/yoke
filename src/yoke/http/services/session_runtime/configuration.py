"""Idle configuration and workspace relocation for one HTTP session."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.session_tree import SessionTree
from yoke.agent.skills import activate_skills, load_skill_registry
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.paths import default_skill_dirs
from yoke.http.errors import ApiError, session_busy, session_not_found
from yoke.http.models.client_session import ClientSessionRequest
from yoke.http.services.client_session import ClientSession
from yoke.http.services.human_input_service import HumanInputService
from yoke.session import SessionRecord
from yoke.session.workspace import (
    WorkspaceBusy,
    WorkspaceConflict,
    relocate_session_workspace,
    require_workspace,
)

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime


class RuntimeConfiguration:
    """Apply idle mutations through the runtime gate and its executor."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self.runtime = runtime
        self.resources = runtime.resources
        self.persistence = runtime.persistence
        self.activity = runtime.activity
        self.store = runtime.store
        self.session_id = runtime.session_id
        self.events = runtime.events

    async def activate_skill(self, skill_name: str) -> ActiveSkill:
        return await self.runtime.idle_mutation(
            partial(self._activate_skill_sync, skill_name)
        )

    async def set_tools(
        self,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        return await self.runtime.idle_mutation(
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

        await self.runtime.idle_mutation(apply)

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
        record = await self.runtime.run_blocking(
            self.store.summary_record, self.session_id
        )
        if record is None:
            raise session_not_found()
        if record.root == target:
            return record
        if expected_root is not None and expected_root != record.root:
            raise WorkspaceConflict(self.session_id, record.root)
        if await self.runtime.run_blocking(self.resources.has_live_work):
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

        updated = await self.runtime.run_blocking(relocate)
        self.runtime.cancel_automatic_title()
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
    runtime: SessionRuntime,
    config: ClientSessionRequest,
    human_input: HumanInputService,
) -> dict[str, object]:
    def configure() -> dict[str, object]:
        if runtime.workers.active_count:
            raise session_busy(
                "Drain native workers before replacing client configuration."
            )
        record = runtime.store.summary_record(runtime.session_id)
        if record is None:
            raise session_not_found()
        runtime.resources.configure_client(
            ClientSession(
                runtime.session_id,
                config,
                human_input,
                runtime.events,
                Path(record.root or Path.cwd()),
            )
        )
        return {
            "mode": config.mode,
            "additionalDirectories": config.additional_directories,
            "mcpServers": [server.name for server in config.mcp_servers],
        }

    return await runtime.idle_mutation(configure)


async def detach_client(runtime: SessionRuntime, owner: str) -> None:
    def detach() -> None:
        if runtime.workers.active_count:
            raise session_busy(
                "Drain native workers before detaching client configuration."
            )
        client = runtime.resources.client_session
        if client is not None and client.config.owner == owner:
            runtime.resources.configure_client(None)

    await runtime.idle_mutation(detach)
