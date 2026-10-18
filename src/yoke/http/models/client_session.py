"""Ephemeral client configuration. Credentials never enter session persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from yoke.http.models.common import ApiModel
from yoke.mcp.config import McpServerConfig

ClientMode = Literal["full-access", "approval-required", "auto-accept-edits", "plan"]


class ClientMcpServer(ApiModel):
    name: str = Field(min_length=1, max_length=128)
    transport: Literal["stdio", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=128)
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_transport(self) -> ClientMcpServer:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP requires a command")
        if self.transport == "streamable-http" and (
            not self.url or not self.url.startswith(("http://", "https://"))
        ):
            raise ValueError("HTTP MCP requires an HTTP URL")
        return self

    def config(self, root: Path) -> McpServerConfig:
        return McpServerConfig(
            name=self.name,
            transport=self.transport,
            command=self.command,
            args=tuple(self.args),
            env=dict(self.env),
            url=self.url,
            headers=dict(self.headers),
            cwd=root,
        )


class ClientSessionRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=128)
    mode: ClientMode = "full-access"
    mcp_servers: list[ClientMcpServer] = Field(default_factory=list, max_length=64)
    additional_directories: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("additional_directories")
    @classmethod
    def absolute_directories(cls, values: list[str]) -> list[str]:
        if any(
            not Path(value).is_absolute() or not Path(value).is_dir()
            for value in values
        ):
            raise ValueError(
                "Additional directories must be existing absolute directories"
            )
        return list(dict.fromkeys(str(Path(value).resolve()) for value in values))

    @model_validator(mode="after")
    def unique_servers(self) -> ClientSessionRequest:
        if len({server.name for server in self.mcp_servers}) != len(self.mcp_servers):
            raise ValueError("MCP server names must be unique")
        return self
