"""Tool for executing Python code with yoke's interpreter."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.tools.command import ManagedCommandTool
from yoke.agent.tools.command_process_types import (
    DEFAULT_EXEC_YIELD_TIME_MS,
)
from yoke.agent.tools.command_process_types import (
    DEFAULT_MAX_OUTPUT_TOKENS,
)
from yoke.agent.tools.python_env import current_python_executable
from yoke.agent.tools.python_env import prepare_python_env
from pydantic import Field


class PythonExecTool(ManagedCommandTool):
    """Execute Python code with yoke's current interpreter."""

    is_yoke_tool = True
    name = "python_exec"
    description = (
        "Execute arbitrary Python code with the current Python interpreter in "
        "the workspace root. Returns output or a session ID for ongoing "
        "execution; use write_stdin to poll streamed output. Child "
        "subprocesses can call `python` or `python3` to use the same "
        "interpreter/venv."
    )

    code: str = Field(min_length=1)
    python_executable: str | None = Field(
        default=None,
        description=(
            "Optional Python executable to use for this call instead of "
            "yoke's current interpreter or active virtual environment."
        ),
    )
    timeout: int = Field(default=180, ge=1)
    yield_time_ms: int = Field(
        default=DEFAULT_EXEC_YIELD_TIME_MS,
        ge=1,
        le=300_000,
        description="Wait before yielding output. Defaults to 30 seconds.",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        description="Approximate output token budget.",
    )

    def execute(self) -> dict[str, object]:
        """Run Python code and return output, status, and timing metadata."""
        try:
            if self._is_cancel_requested():
                return self._cancelled_result()
            env = self._manager().base_environment()
            python_executable = self._python_executable()
            prepare_python_env(env, python_executable)
            env["PYTHONIOENCODING"] = "utf-8:replace"
            env.setdefault("PYTHONUTF8", "1")
            result = self._manager().exec_argv(
                argv=[python_executable, "-u", "-c", self.code],
                display_command=f"{python_executable} -u -c <code>",
                cwd=self.root,
                env=env,
                yield_time_ms=self.yield_time_ms,
                timeout_seconds=self.timeout,
                cancel_requested=self._is_cancel_requested,
            )
        except Exception as exc:
            return self._error(str(exc))
        payload = self._format_result(
            result,
            max_output_tokens=(self.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS),
        )
        payload["python_executable"] = python_executable
        payload["timeout"] = self.timeout
        if result.timed_out:
            payload["error"] = (
                f"Python execution timed out after {self.timeout} seconds"
            )
        return payload

    def _python_executable(self) -> str:
        """Return the interpreter selected for this tool call."""
        if self.python_executable is None:
            return current_python_executable()
        return str(Path(self.python_executable).expanduser().resolve(strict=False))
