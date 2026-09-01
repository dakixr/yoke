"""MCP config inspection without exposing credentials or command details."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

from yoke.http.errors import ApiError
from yoke.http.models.common import LocationInfo
from yoke.http.models.mcp import McpListResponse
from yoke.http.models.mcp import McpServerInfo
from yoke.http.models.mcp import McpToolInfo
from yoke.http.services.event_broker import EventService
from yoke.http.services.redaction import redact_public_value
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.session import SessionStore

if TYPE_CHECKING:
    from yoke.mcp.config import McpSessionPolicy


class McpService:
    """Project MCP config and optional live tool inspection."""

    def __init__(
        self,
        store: SessionStore,
        runtimes: SessionRuntimeRegistry,
        events: EventService,
        home: Path | None = None,
    ) -> None:
        self.store = store
        self.runtimes = runtimes
        self.events = events
        self.home = (home or Path.home()).resolve()

    def list(
        self,
        *,
        directory: str | None,
        session_id: str | None,
        include_tools: bool,
    ) -> McpListResponse:
        root = self._root(directory=directory, session_id=session_id)
        policy = self._policy(session_id)
        from yoke.mcp.config import load_mcp_config

        config = load_mcp_config(root=root, home=self.home, session_policy=policy)
        inspected: dict[str, dict[str, object]] = {}
        if include_tools and config.enabled_servers:
            from yoke.mcp import McpManager

            manager = McpManager.from_paths(
                root=root,
                home=self.home,
                session_policy=policy,
            )
            try:
                payload = manager.inspect(include_schemas=True)
            finally:
                manager.close()
            servers = payload.get("servers") if isinstance(payload, dict) else None
            if isinstance(servers, list):
                for item in servers:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    if isinstance(name, str):
                        inspected[name] = cast(dict[str, object], item)
        data = [
            self._server_info(
                server,
                root=root,
                inspection=inspected.get(server.name),
            )
            for server in config.servers
        ]
        return McpListResponse(
            location=LocationInfo(directory=str(root)),
            data=data,
        )

    def configured_server(
        self,
        session_id: str,
        server_name: str,
    ) -> McpServerInfo:
        response = self.list(
            directory=None,
            session_id=session_id,
            include_tools=False,
        )
        for item in response.data:
            if item.name == server_name:
                return item
        raise ApiError(404, "mcp_server_not_found", "MCP server was not found.")

    def patch_persisted(
        self,
        session_id: str,
        server_name: str,
        *,
        scope: str,
        enabled: bool | None,
        enabled_tools: tuple[str, ...] | None,
        disabled_tools: tuple[str, ...] | None,
        update_enabled_tools: bool,
        update_disabled_tools: bool,
    ) -> McpServerInfo:
        """Persist repo/global policy using the shared MCP config editor."""
        root = self._root(directory=None, session_id=session_id)
        from yoke.mcp.config import load_mcp_config
        from yoke.mcp.editing import patch_persisted_mcp_server

        config = load_mcp_config(root=root, home=self.home)
        server = next(
            (item for item in config.servers if item.name == server_name), None
        )
        if server is None:
            raise ApiError(404, "mcp_server_not_found", "MCP server was not found.")
        try:
            patch_persisted_mcp_server(
                root=root,
                home=self.home,
                scope=scope,
                server=server,
                enabled=enabled,
                enabled_tools=enabled_tools,
                disabled_tools=disabled_tools,
                update_enabled_tools=update_enabled_tools,
                update_disabled_tools=update_disabled_tools,
            )
        except (OSError, ValueError) as exc:
            raise ApiError(
                400,
                "mcp_config_update_failed",
                f"Could not update MCP config: {exc}",
            ) from exc
        self.events.live(
            "catalog.updated",
            {"catalog": "mcp", "server": server_name, "scope": scope},
            session_id=session_id,
            location=str(root),
        )
        return self.configured_server(session_id, server_name)

    def _root(self, *, directory: str | None, session_id: str | None) -> Path:
        if session_id is not None:
            record = self.store.summary_record(session_id)
            if record is None:
                raise ApiError(404, "session_not_found", "Session was not found.")
            return Path(record.root or Path.cwd()).resolve()
        root = Path(directory or Path.cwd()).resolve()
        if not root.is_dir():
            raise ApiError(
                404, "location_not_found", "Location directory was not found."
            )
        return root

    def _policy(self, session_id: str | None) -> McpSessionPolicy | None:
        if session_id is None:
            return None
        runtime = self.runtimes.get_if_loaded(session_id)
        return runtime.mcp_session_policy() if runtime is not None else None

    def _server_info(self, server, *, root: Path, inspection) -> McpServerInfo:  # noqa: ANN001
        source_path = server.source_path
        global_path = (self.home / ".yoke" / "mcp.json").resolve()
        repo_path = (root / ".yoke" / "mcp.json").resolve()
        resolved_source = source_path.resolve() if source_path is not None else None
        if resolved_source == global_path:
            scope = "global"
        elif resolved_source == repo_path:
            scope = "repo"
        else:
            scope = "unknown"
        status = "configured"
        error: str | None = None
        tools: list[McpToolInfo] = []
        truncated = False
        if isinstance(inspection, dict):
            status_value = inspection.get("status")
            status = status_value if isinstance(status_value, str) else status
            error_value = redact_public_value(inspection.get("error"))
            error = error_value if isinstance(error_value, str) else None
            tool_values = inspection.get("tools")
            if isinstance(tool_values, list):
                for tool in tool_values:
                    if not isinstance(tool, dict) or not isinstance(
                        tool.get("name"), str
                    ):
                        continue
                    schema = redact_public_value(tool.get("input_schema"))
                    tools.append(
                        McpToolInfo(
                            name=cast(str, tool["name"]),
                            description=(
                                cast(str, tool["description"])
                                if isinstance(tool.get("description"), str)
                                else None
                            ),
                            input_schema=(
                                cast(dict[str, object], schema)
                                if isinstance(schema, dict)
                                else None
                            ),
                        )
                    )
            truncated = bool(inspection.get("truncated", False))
        return McpServerInfo(
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            scope=scope,
            source_path=str(source_path) if source_path is not None else None,
            status=status,
            error=error,
            enabled_tools=(
                list(server.enabled_tools) if server.enabled_tools is not None else None
            ),
            disabled_tools=list(server.disabled_tools),
            tools=tools,
            truncated=truncated,
        )
