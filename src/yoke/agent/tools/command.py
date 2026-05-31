"""Tool for executing shell commands in the workspace root."""

from __future__ import annotations

import os
import subprocess
import time

from pydantic import AliasChoices
from pydantic import Field

from yoke.agent.tools.python_env import current_python_executable
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.truncate import truncate_tail

from .base import WorkspaceTool
from .shell import COMMAND_TOOL_NAME
from .shell import build_shell_command


def decode_command_output(raw: bytes) -> str:
    """Decode raw command output bytes to a normalized string."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return (
        raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    )


class CommandTool(WorkspaceTool):
    """Tool that runs a shell command in the workspace root."""

    name = COMMAND_TOOL_NAME
    description = (
        f"Run a {COMMAND_TOOL_NAME} command in the workspace root. "
        "`python` and `python3` resolve to yoke's current interpreter/venv. "
        "Output keeps the last 2000 lines or 50KB."
    )

    command: str = Field(min_length=1)
    timeout: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("timeout", "timeout_seconds"),
    )

    def execute(self) -> dict[str, object]:
        """Run the command and return output with exit code and truncation."""
        started_at = time.perf_counter()
        try:
            env = os.environ.copy()
            prepare_python_env(env)
            process = subprocess.Popen(  # noqa: S603
                build_shell_command(self.command, env),
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                env=env,
                start_new_session=os.name != "nt",
            )
            if process.stdin is not None:
                process.stdin.write(self.command.encode("utf-8"))
                process.stdin.close()
                process.stdin = None
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                if self._is_cancel_requested():
                    return self._cancel_process(
                        process,
                        elapsed_seconds=time.perf_counter() - started_at,
                    )
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return self._timeout_process(
                        process,
                        elapsed_seconds=time.perf_counter() - started_at,
                    )
                try:
                    stdout_raw, stderr_raw = process.communicate(
                        timeout=0.05 if remaining is None else min(0.05, remaining)
                    )
                except subprocess.TimeoutExpired:
                    continue
                elapsed_seconds = time.perf_counter() - started_at
                return self._build_result(
                    returncode=process.returncode or 0,
                    stdout=decode_command_output(stdout_raw),
                    stderr=decode_command_output(stderr_raw),
                    timed_out=False,
                    elapsed_seconds=elapsed_seconds,
                )
        except Exception as exc:
            return self._error(str(exc), command=self.command, timeout=self.timeout)

    def _cancel_process(
        self, process: subprocess.Popen[bytes], *, elapsed_seconds: float | None = None
    ) -> dict[str, object]:
        if elapsed_seconds is None:
            elapsed_seconds = 0.0
        self._terminate_process(process)
        stdout_raw, stderr_raw = process.communicate()
        result = self._build_result(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=decode_command_output(stdout_raw),
            stderr=decode_command_output(stderr_raw),
            timed_out=False,
            elapsed_seconds=elapsed_seconds,
        )
        result["ok"] = False
        result["error"] = "Command cancelled"
        result["cancelled"] = True
        return result

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name != "nt" and process.pid is not None:
            try:
                os.killpg(process.pid, 15)
            except ProcessLookupError:
                return
            except OSError:
                process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            if os.name != "nt" and process.pid is not None:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    return
                except OSError:
                    process.kill()
            else:
                process.kill()
            process.wait()

    def _timeout_process(
        self, process: subprocess.Popen[bytes], *, elapsed_seconds: float | None = None
    ) -> dict[str, object]:
        if elapsed_seconds is None:
            elapsed_seconds = float(self.timeout or 0)
        self._terminate_process(process)
        stdout_raw, stderr_raw = process.communicate()
        result = self._build_result(
            returncode=-1,
            stdout=decode_command_output(stdout_raw),
            stderr=decode_command_output(stderr_raw),
            timed_out=True,
            elapsed_seconds=elapsed_seconds,
        )
        result["ok"] = False
        result["error"] = f"Command timed out after {self.timeout} seconds"
        return result

    def _build_result(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
        elapsed_seconds: float,
    ) -> dict[str, object]:
        combined_parts = [part.rstrip("\n") for part in (stdout, stderr) if part]
        combined_output = "\n".join(part for part in combined_parts if part)
        combined_truncation = truncate_tail(combined_output)
        output_view_dict = combined_truncation.to_dict()
        output_view_dict.pop("content")
        payload: dict[str, object] = {
            "ok": returncode == 0 and not timed_out,
            "python_executable": current_python_executable(),
            "returncode": returncode,
            "timeout": self.timeout,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed_seconds,
            "output": combined_truncation.content.rstrip("\n"),
            "outputTruncationDetails": output_view_dict,
        }
        if returncode != 0:
            payload["error"] = f"Command exited with status {returncode}"
        return payload
