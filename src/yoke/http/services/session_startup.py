"""CLI-compatible startup resolution and a transport-safe discovery report."""

from __future__ import annotations

import os
from pathlib import Path

from yoke.agent.provider_selection import capture_provider_session_state
from yoke.cli.config import CLIArgs
from yoke.cli.providers.state import apply_session_defaults_to_args
from yoke.cli.config.runtime import (
    build_cli_agent_from_args,
    format_tool_discovery_message,
)
from yoke.environment import execution_environment, project_environment
from yoke.http.models.session import SessionCreateRequest, SessionStartupReport
from yoke.http.runtime.ownership import close_owned_agent
from yoke.http.services.privacy import SessionPrivacy
from yoke.session import SessionStore


class SessionStartup:
    """Resolve flags through the existing CLI resolver, never a second policy."""

    def __init__(self, store: SessionStore, privacy: SessionPrivacy) -> None:
        self.store = store
        self.privacy = privacy

    def initialize(
        self, session_id: str, request: SessionCreateRequest
    ) -> SessionStartupReport:
        selection = request.selection
        args = CLIArgs(
            session=session_id,
            root=request.location.directory,
            skills=tuple(request.skills),
            headless=request.headless,
            provider_name=selection.provider if selection else None,
            model=selection.model if selection else None,
            reasoning_effort=selection.reasoning_effort if selection else None,
        )
        record = self.store.load(session_id)
        apply_session_defaults_to_args(args, record)
        existing_names = {skill.name for skill in record.active_skills}
        args.skills = tuple(name for name in args.skills if name not in existing_names)
        client = request.client
        environment = project_environment(
            Path(args.root),
            client.env if client is not None and client.env is not None else os.environ,
            cwd=Path(client.cwd)
            if client is not None and client.cwd is not None
            else None,
        )
        self.privacy.register(environment.values)
        with execution_environment(environment), self.privacy.scope():
            built = build_cli_agent_from_args(args, recover_model=True)
        try:
            state = capture_provider_session_state(built.agent)
            record = self.store.load(session_id)
            assert record is not None
            self.store.save(
                session_id,
                [],
                conversation_entries=record.conversation_entries,
                leaf_id=record.leaf_id,
                root=record.root,
                title=record.title,
                active_skills=[*record.active_skills, *built.agent.active_skills],
                provider_name=state.provider_name,
                model_id=state.model_id,
                reasoning_effort=state.reasoning_effort,
                context_window_tokens=state.context_window_tokens,
                existing_record=record,
            )
            report = built.tool_report
            return SessionStartupReport(
                warnings=[self.privacy.text(built.startup_warning)]
                if built.startup_warning
                else [],
                tool_report={
                    "message": format_tool_discovery_message(report),
                    "counts": {
                        source: report.count(source)
                        for source in ("default", "repo", "global")
                    },
                    "activeTools": [tool.tool.name for tool in report.active_tools],
                    "deniedTools": [tool.tool.name for tool in report.denied_tools],
                    "configPath": str(report.config_path)
                    if report.config_path
                    else None,
                    "unmatchedToolNames": report.unmatched_tool_names or [],
                    "unmatchedCapabilityIDs": report.unmatched_capability_ids or [],
                },
                active_skills=[
                    skill.name
                    for skill in [*record.active_skills, *built.agent.active_skills]
                ],
                model_source=args.model_source,
            )
        finally:
            close_owned_agent(built.agent, description="HTTP startup discovery")
