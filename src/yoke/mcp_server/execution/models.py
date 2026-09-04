"""Typed MCP-only requests and result contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yoke.agent.tools.read import ReadTool
from yoke.mcp_server.search import MCPFdTool, MCPRipgrepTool


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadItem(Request):
    id: str = Field(min_length=1, max_length=80)
    tool: Literal["read_file"]
    arguments: ReadTool


class SearchItem(Request):
    id: str = Field(min_length=1, max_length=80)
    tool: Literal["rg"]
    arguments: MCPRipgrepTool


class FindItem(Request):
    id: str = Field(min_length=1, max_length=80)
    tool: Literal["fd"]
    arguments: MCPFdTool


class BatchRead(Request):
    items: list[
        Annotated[Union[ReadItem, SearchItem, FindItem], Field(discriminator="tool")]
    ] = Field(min_length=1, max_length=16)
    max_concurrency: int = Field(default=4, ge=1, le=4)
    deadline_ms: int = Field(default=30000, ge=1, le=30000)
    max_output_tokens: int = Field(default=8000, ge=512, le=16000)

    @model_validator(mode="after")
    def unique_ids(self) -> BatchRead:
        if self.max_output_tokens * 4 < 1024 + 512 * len(self.items):
            raise ValueError(
                "Output budget must reserve at least 512 bytes per item plus 1024 bytes for the envelope"
            )
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Item IDs must be unique")
        for item in self.items:
            if item.tool == "rg" and any(
                a == "--pre" or a.startswith("--pre=")
                for a in item.arguments._parse_raw_args()
            ):
                raise ValueError("rg --pre is not a read operation")
            if item.tool == "fd":
                from yoke.mcp_server.search import _is_fd_execution_argument

                if any(
                    _is_fd_execution_argument(a)
                    for a in item.arguments._parse_raw_args()
                ):
                    raise ValueError("fd execution is not a read operation")
        return self


class ResultRead(Request):
    result_ref: str
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=64000)
    fields: list[str] | None = Field(default=None, max_length=32)


class Inspect(Request):
    refresh: bool = False
    server: str | None = None
    query: str | None = None
    queries: list[str] = Field(default_factory=list, max_length=16)
    tools: list[str] = Field(default_factory=list, max_length=32)
    include_schemas: bool = False
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class DownstreamCall(Request):
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    schema_hash: str | None = None
    fields: list[str] | None = Field(default=None, max_length=32)
    max_output_tokens: int = Field(default=8000, ge=512, le=16000)


class ResultEnvelope(BaseModel):
    ok: bool
    model_config = ConfigDict(extra="allow")
