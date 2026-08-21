"""Explicit allowlist of Yoke tools exposed over MCP."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.types import ToolAnnotations

from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.command import ExecCommandTool
from yoke.agent.tools.command import WriteStdinTool
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.agent.tools.read import ReadTool
from yoke.agent.tools.search import LsTool
from yoke.mcp_server.tools import FindFilesTool
from yoke.mcp_server.tools import SearchTextTool


@dataclass(frozen=True, slots=True)
class ExposedTool:
    """Stable external metadata for one explicitly exposed Yoke tool."""

    name: str
    title: str
    description: str
    tool_class: type[LocalTool]
    annotations: ToolAnnotations


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
            "list_files",
            "List files",
            "List files and directories on the server. Relative paths resolve "
            "from the configured default root; absolute paths are allowed.",
            LsTool,
            READ_ONLY,
        ),
        ExposedTool(
            "search_text",
            "Search text",
            SearchTextTool.description,
            SearchTextTool,
            READ_ONLY,
        ),
        ExposedTool(
            "find_files",
            "Find files",
            FindFilesTool.description,
            FindFilesTool,
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
            "exec_command",
            "Execute command",
            "Execute a shell command on the server for builds, tests, Git, "
            "service inspection, package managers, and other terminal tasks. "
            "Returns final output if it finishes within the yield window, "
            "otherwise a process session ID for process_io.",
            ExecCommandTool,
            EXECUTION,
        ),
        ExposedTool(
            "exec_python",
            "Execute Python",
            "Execute Python with Yoke's current interpreter and environment. "
            "Long-running calls return a session ID for process_io.",
            PythonExecTool,
            EXECUTION,
        ),
        ExposedTool(
            "process_io",
            "Process input/output",
            "Continue a live exec_command or exec_python process using its "
            "session ID. Pass empty chars to poll recent output; pass non-empty "
            "chars to write to the process stdin.",
            WriteStdinTool,
            EXECUTION,
        ),
    )
}
