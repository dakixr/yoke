"""Typed remote tool and MCP catalog operations."""

from __future__ import annotations

import logging

import httpx

from collections.abc import Sequence
from pathlib import Path

from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.daemon.presentation import DaemonSessionRecord
from yoke.cli.providers.catalog import ProviderModelChoice
from yoke.ai.providers.base import ProviderModelInfo
from yoke.http.models.catalog import ModelListResponse
from yoke.http.models.mcp import McpListResponse
from yoke.http.models.tool import ToolInfo, ToolListResponse
from yoke.mcp.config import McpConfig, McpServerConfig
from yoke.mcp.manager import McpToolInfo


class DaemonToolInventory:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def report(self) -> list[ToolInfo]:
        return ToolListResponse.model_validate(
            self.connection.request(
                "GET", "tool", params={"sessionID": self.connection.session_id}
            )
        ).data

    def active_names(self) -> set[str]:
        return {item.name for item in self.report() if item.enabled}

    def select(
        self,
        active_names: set[str],
        visible_names: set[str],
        report: list[ToolInfo],
        *,
        ordered_names: Sequence[str],
    ) -> None:
        del report, ordered_names
        self.connection.session_request(
            "PATCH",
            "/tool",
            json={
                "enabled": sorted(active_names),
                "disabled": sorted(visible_names - active_names),
            },
        )


class DaemonMcpInventory:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def config(self) -> McpConfig:
        data = self.read()
        return McpConfig(
            servers=tuple(
                McpServerConfig(
                    name=item.name,
                    transport=item.transport,
                    enabled=item.enabled,
                    source_path=Path(item.source_path) if item.source_path else None,
                    enabled_tools=tuple(item.enabled_tools)
                    if item.enabled_tools is not None
                    else None,
                    disabled_tools=tuple(item.disabled_tools),
                )
                for item in data.data
            )
        )

    def read(self) -> McpListResponse:
        return McpListResponse.model_validate(
            self.connection.session_request(
                "GET", "/mcp", params={"includeTools": True}
            )
        )

    def list_tools(self, server: McpServerConfig) -> tuple[McpToolInfo, ...]:
        for item in self.read().data:
            if item.name == server.name:
                return tuple(
                    McpToolInfo(
                        name=value.name,
                        description=value.description or "",
                        input_schema=value.input_schema or {},
                    )
                    for value in item.tools
                )
        return ()

    def set_enabled(self, server: McpServerConfig, enabled: bool) -> None:
        self.connection.session_request(
            "PATCH",
            f"/mcp/{server.name}",
            json={"enabled": enabled, "scope": "session"},
        )

    def toggle_tool(self, server: McpServerConfig, tool_name: str) -> None:
        disabled = set(server.disabled_tools)
        enabled = (
            set(server.enabled_tools) if server.enabled_tools is not None else None
        )
        if enabled is not None:
            if tool_name in enabled:
                enabled.remove(tool_name)
            else:
                enabled.add(tool_name)
        elif tool_name in disabled:
            disabled.remove(tool_name)
        else:
            disabled.add(tool_name)
        self.connection.session_request(
            "PATCH",
            f"/mcp/{server.name}",
            json={
                "enabledTools": sorted(enabled) if enabled is not None else None,
                "disabledTools": sorted(disabled),
                "scope": "session",
            },
        )

    def refresh(self) -> None:
        self.read()


class DaemonModelCatalog:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def choices(self, root: Path) -> list[ProviderModelChoice]:
        values = ModelListResponse.model_validate(
            self.connection.request("GET", "model", params={"directory": str(root)})
        ).data
        return [
            ProviderModelChoice(
                provider_name=item.provider,
                model=ProviderModelInfo(
                    id=item.id,
                    display_name=item.name,
                    context_window_tokens=item.context_window_tokens,
                    thinking_levels=tuple(item.reasoning_efforts),
                    default_thinking_level=item.default_reasoning_effort,
                    supports_image_inputs=item.capabilities.images,
                ),
            )
            for item in values
        ]

    def thinking(
        self, root: Path, selection: DaemonSessionRecord
    ) -> tuple[str | None, tuple[str, ...]]:
        try:
            models = ModelListResponse.model_validate(
                self.connection.request(
                    "GET",
                    "model",
                    params={
                        "provider": selection.provider_name,
                        "directory": str(root),
                    },
                )
            ).data
        except (DaemonRequestError, httpx.HTTPError) as exc:
            logging.getLogger(__name__).warning(
                "Could not read daemon thinking levels: %s", exc
            )
            return selection.reasoning_effort, ()
        levels = next(
            (
                tuple(model.reasoning_efforts)
                for model in models
                if model.id == selection.model_id
            ),
            (),
        )
        return selection.reasoning_effort, levels
