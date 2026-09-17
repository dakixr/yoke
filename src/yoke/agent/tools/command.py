"""Tools for managed command execution and background interaction."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Self

from pydantic import AliasChoices
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from yoke.agent.tools.processes.base import ManagedCommandTool as ManagedCommandTool
from yoke.agent.tools.processes.base import ManagedExecutionTool
from yoke.agent.tools.processes import ProcessInputTool
from yoke.agent.tools.python_env import prepare_python_env


class ExecCommandTool(ManagedExecutionTool):
    """Run exactly one shell command or direct argv process."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["cmd"]},
                {"required": ["argv"]},
            ]
        }
    )

    name = "command_exec"
    provider_result_projection = "command"
    description = (
        "Run either a shell command or direct argv process, returning output "
        "and a retained session ID. Auto uses the host's normal initial completion "
        "wait; background returns promptly for concurrent or interactive work. Use "
        "process_read with the returned opaque cursor to wait or read remaining output, "
        "process_input to send stdin, and process_cancel to stop work. Shell "
        "commands default to PowerShell on Windows."
    )

    cmd: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("cmd", "command"),
        description="Shell command to execute. Mutually exclusive with argv.",
    )
    argv: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Direct process arguments. Bypasses shell parsing and is mutually "
            "exclusive with cmd."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description="Working directory. Defaults to the workspace root.",
    )
    tty: bool = Field(
        default=False,
        description="Allocate a PTY for interactive terminal input.",
    )
    shell: str | None = Field(
        default=None,
        description="Shell executable. Defaults to the user's shell.",
    )
    login: bool = Field(
        default=True,
        description="Use login shell semantics for cmd where supported.",
    )

    @model_validator(mode="after")
    def _validate_execution_mode(self) -> Self:
        if (self.cmd is None) == (self.argv is None):
            raise ValueError("Provide exactly one of cmd or argv.")
        if self.argv is not None and any(not value for value in self.argv):
            raise ValueError("argv entries must be non-empty strings.")
        if self.argv is not None and self.shell is not None:
            raise ValueError("shell cannot be combined with argv.")
        return self

    def execute(self) -> dict[str, object]:
        """Start a command once and return a cursor into retained output."""
        try:
            if self._is_cancel_requested():
                return self._cancelled_result()
            cwd = self._resolve_workdir()
            if self.argv is not None:
                env = self._manager().base_environment()
                prepare_python_env(env)
                return self._start(
                    argv=list(self.argv),
                    command=shlex.join(self.argv),
                    cwd=cwd,
                    env=env,
                    tty=self.tty,
                    timeout_seconds=None,
                )
            else:
                assert self.cmd is not None
                return self._start(
                    command=self.cmd,
                    cwd=cwd,
                    tty=self.tty,
                    shell=self.shell,
                    login=self.login,
                )
        except Exception as exc:
            return self._error(str(exc))

    def _resolve_workdir(self) -> Path:
        if self.workdir is None:
            return self._require_live_root()
        cwd = self._resolve_path(self.workdir)
        if not cwd.is_dir():
            raise NotADirectoryError(str(cwd))
        return cwd


CommandExecTool = ExecCommandTool
CommandTool = ExecCommandTool
# Python import compatibility only. The registered tool is process_input.
WriteStdinTool = ProcessInputTool
