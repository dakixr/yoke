"""Explicit allowlist of Yoke tools exposed over MCP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mcp.types import ToolAnnotations

from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.mcp import McpCallTool
from yoke.agent.tools.mcp import McpInspectTool
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.agent.tools.read import ReadTool
from yoke.mcp_server.files import MCPViewImageTool
from yoke.mcp_server.commands import MCPExecCommandTool, MCPProcessInputTool
from yoke.mcp_server.search import MCPFdTool
from yoke.mcp_server.search import MCPRipgrepTool
from yoke.mcp_server.skills import MCPSkillTool


@dataclass(frozen=True, slots=True)
class ExposedTool:
    """Stable external metadata for one explicitly exposed Yoke tool."""

    name: str
    title: str
    description: str
    tool_class: type[LocalTool]
    annotations: ToolAnnotations
    result_kind: Literal["json", "image"] = "json"


READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
EXECUTION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


TOOL_REGISTRY = {
    spec.name: spec
    for spec in (
        ExposedTool(
            "read_file",
            "Read file",
            "Read a UTF-8 text file on the server. Relative paths resolve from "
            "the configured default root; absolute paths are allowed. Use offset "
            "and limit to continue large files.",
            ReadTool,
            READ_ONLY,
        ),
        ExposedTool(
            "view_image",
            "View image",
            MCPViewImageTool.description,
            MCPViewImageTool,
            READ_ONLY,
            "image",
        ),
        ExposedTool(
            "rg",
            "Ripgrep",
            MCPRipgrepTool.description,
            MCPRipgrepTool,
            READ_ONLY,
        ),
        ExposedTool(
            "fd",
            "Find files",
            MCPFdTool.description,
            MCPFdTool,
            READ_ONLY,
        ),
        ExposedTool(
            "skill",
            "Load skill",
            MCPSkillTool.description,
            MCPSkillTool,
            READ_ONLY,
        ),
        ExposedTool(
            "apply_patch",
            "Apply patch",
            "Create, update, move, or delete files using a Codex-style patch. "
            "Paths follow normal Yoke/server path semantics.",
            ApplyPatchTool,
            MUTATION,
        ),
        ExposedTool(
            "command_exec",
            "Execute command",
            "Execute a shell command on the server for builds, tests, Git, "
            "service inspection, package managers, and other terminal tasks. "
            'Use cmd for shell text, for example {"cmd":"pwd"}, or argv for '
            'direct arguments, for example {"argv":["pwd"]}. Provide exactly '
            "one. cmd must be a string, never an array. On INVALID_ARGUMENT, "
            "correct the arguments and retry. No command started; do not report "
            "a permission denial without an actual denial response. "
            "Auto mode uses the host's normal initial completion wait. Background mode "
            "returns immediately. Every started process retains a session ID and opaque output cursor, even "
            "after completion. Use process_read for waiting and remaining output.",
            MCPExecCommandTool,
            EXECUTION,
        ),
        ExposedTool(
            "python_exec",
            "Execute Python",
            "Execute Python with Yoke's current interpreter and environment. "
            "Auto mode uses the host's normal initial completion wait. Background mode returns immediately. "
            "Use process_read to wait or page output, process_input to send stdin.",
            PythonExecTool,
            EXECUTION,
        ),
        ExposedTool(
            "process_input",
            "Process input",
            "Send required nonempty chars to a process session, then collect output "
            "for up to wait_ms, default 250 and maximum 5000. Pass the last cursor "
            "unchanged to avoid replaying output. Without a cursor, reads start at the earliest "
            "retained output. Use process_read for observation without input.",
            MCPProcessInputTool,
            EXECUTION,
        ),
    )
}


DOWNSTREAM_MCP_TOOL_REGISTRY = {
    spec.name: spec
    for spec in (
        ExposedTool(
            "mcp_inspect",
            "Inspect downstream MCP",
            McpInspectTool.description,
            McpInspectTool,
            READ_ONLY,
        ),
        ExposedTool(
            "mcp_call",
            "Call downstream MCP tool",
            McpCallTool.description,
            McpCallTool,
            EXECUTION,
        ),
    )
}


def effective_tool_registry() -> dict[str, ExposedTool]:
    """Return the exact externally callable tool registry for one service."""
    return {**TOOL_REGISTRY, **DOWNSTREAM_MCP_TOOL_REGISTRY}
