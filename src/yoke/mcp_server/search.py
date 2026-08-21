"""Read-only MCP variants of Yoke's native rg and fd tools."""

from __future__ import annotations

from yoke.agent.tools.fd import FdTool
from yoke.agent.tools.rg import RipgrepTool


class MCPRipgrepTool(RipgrepTool):
    """Expose native rg arguments without its subprocess preprocessor hook."""

    description = (
        "Run ripgrep using raw_args containing the exact arguments to pass after "
        "`rg`. Prefer rg for text search and file listing. The subprocess-running "
        "`--pre` option is disabled; use exec_command for command execution."
    )

    def execute(self) -> dict[str, object]:
        """Reject subprocess hooks, then execute native ripgrep."""
        arguments = self._parse_raw_args()
        if any(
            argument == "--pre" or argument.startswith("--pre=")
            for argument in arguments
        ):
            return self._error(
                "rg --pre is disabled in the read-only MCP tool; use exec_command"
            )
        return super().execute()


class MCPFdTool(FdTool):
    """Expose native fd arguments without its command-execution switches."""

    description = (
        "Run fd using raw_args containing the exact arguments to pass after "
        "`fd`. Use it for recursive listing and file or directory discovery by "
        "name, path, glob, regex, type, extension, depth, or ignore rules. "
        "Command-execution options are disabled; use exec_command instead."
    )

    def execute(self) -> dict[str, object]:
        """Reject command-execution switches, then execute native fd."""
        arguments = self._parse_raw_args()
        if any(_is_fd_execution_argument(argument) for argument in arguments):
            return self._error(
                "fd command-execution options are disabled in the read-only MCP "
                "tool; use exec_command"
            )
        return super().execute()


def _is_fd_execution_argument(argument: str) -> bool:
    if argument in {"--exec", "--exec-batch"}:
        return True
    if argument.startswith(("--exec=", "--exec-batch=")):
        return True
    return (
        argument.startswith("-")
        and not argument.startswith("--")
        and any(flag in argument[1:] for flag in ("x", "X"))
    )
