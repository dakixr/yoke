"""Tool discovery and session-only enablement projections."""

from __future__ import annotations

from pathlib import Path

from yoke.http.errors import ApiError
from yoke.http.models.common import LocationInfo
from yoke.http.models.tool import ToolInfo
from yoke.http.models.tool import ToolListResponse
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.session import SessionStore


class ToolService:
    """Discover tools without constructing a model provider."""

    def __init__(
        self,
        store: SessionStore,
        runtimes: SessionRuntimeRegistry,
    ) -> None:
        self.store = store
        self.runtimes = runtimes

    def inventory(
        self,
        *,
        directory: str | None,
        session_id: str | None,
    ) -> ToolListResponse:
        from yoke.cli.bootstrap.config import resolve_agent_config

        root = self._root(directory=directory, session_id=session_id)
        report = resolve_agent_config(
            root=root,
            base_system_prompt=None,
            include_agents_file=False,
        ).tool_report
        default_enabled = {item.tool.name for item in report.active_tools}
        enabled_names = default_enabled
        if session_id is not None:
            runtime = self.runtimes.get_if_loaded(session_id)
            if runtime is not None:
                override = runtime.session_enabled_tool_names()
                if override is not None:
                    enabled_names = override
        data = [
            ToolInfo(
                name=item.tool.name,
                description=item.tool.description,
                enabled=item.tool.name in enabled_names,
                source=item.source_kind,
                source_path=(
                    str(item.source_path) if item.source_path is not None else None
                ),
                capability_id=item.capability_id,
                input_schema=item.tool.__class__.model_json_schema(by_alias=True),
            )
            for item in report.discovered_tools
        ]
        data.sort(key=lambda item: (item.source, item.name))
        return ToolListResponse(
            location=LocationInfo(directory=str(root)),
            data=data,
        )

    def discovered_names(self, session_id: str) -> tuple[set[str], set[str]]:
        from yoke.cli.bootstrap.config import resolve_agent_config

        record = self._require_record(session_id)
        root = Path(record.root or Path.cwd()).resolve()
        report = resolve_agent_config(
            root=root,
            base_system_prompt=None,
            include_agents_file=False,
        ).tool_report
        return (
            {item.tool.name for item in report.discovered_tools},
            {item.tool.name for item in report.active_tools},
        )

    def _root(self, *, directory: str | None, session_id: str | None) -> Path:
        if session_id is not None:
            record = self._require_record(session_id)
            return Path(record.root or Path.cwd()).resolve()
        root = Path(directory or Path.cwd()).resolve()
        if not root.is_dir():
            raise ApiError(
                404, "location_not_found", "Location directory was not found."
            )
        return root

    def _require_record(self, session_id: str):  # noqa: ANN202
        record = self.store.summary_record(session_id)
        if record is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        return record
