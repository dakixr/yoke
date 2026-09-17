"""Tool for executing Python code with yoke's interpreter."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.tools.processes.base import ManagedExecutionTool
from yoke.agent.tools.python_env import current_python_executable
from yoke.agent.tools.python_env import prepare_python_env
from pydantic import Field


class PythonExecTool(ManagedExecutionTool):
    """Execute Python code with yoke's current interpreter."""

    is_yoke_tool = True
    name = "python_exec"
    provider_result_projection = "command"
    description = (
        "Execute arbitrary Python code with the current Python interpreter in "
        "the workspace root. Auto uses the host's normal initial completion wait; background "
        "returns promptly. Use process_read with the returned opaque cursor to wait "
        "or read remaining output, and process_input to send stdin. Child "
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
            payload = self._start(
                argv=[python_executable, "-u", "-c", self.code],
                command=f"{python_executable} -u -c <code>",
                cwd=self._require_live_root(),
                env=env,
                timeout_seconds=self.timeout,
            )
        except Exception as exc:
            return self._error(str(exc))
        payload["python_executable"] = python_executable
        payload["timeout"] = self.timeout
        if payload.get("timed_out"):
            payload["error"] = (
                f"Python execution timed out after {self.timeout} seconds"
            )
        return payload

    def _python_executable(self) -> str:
        """Return the interpreter selected for this tool call."""
        if self.python_executable is None:
            return current_python_executable()
        return str(Path(self.python_executable).expanduser().resolve(strict=False))
