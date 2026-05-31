from __future__ import annotations

import os
import subprocess
import time

from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.python_env import current_python_executable
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.truncate import truncate_tail
from pydantic import Field


class PythonExecTool(WorkspaceTool):
    is_yoke_tool = True
    name = "python_exec"
    description = (
        "Execute arbitrary Python code with the current Python interpreter in the "
        "workspace root. Returns stdout, stderr, combined output, exit status, and runtime."
        "Child subprocesses can call `python` or `python3` to use the same interpreter/venv."
    )

    code: str = Field(min_length=1)
    timeout: int = Field(default=180, ge=1)

    def execute(self) -> dict[str, object]:
        started_at = time.perf_counter()

        env = os.environ.copy()
        prepare_python_env(env)
        env["PYTHONIOENCODING"] = "utf-8:replace"
        env.setdefault("PYTHONUTF8", "1")
        python_executable = current_python_executable()

        try:
            completed = subprocess.run(
                [python_executable, "-c", self.code],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_seconds = time.perf_counter() - started_at
            stdout = self._decode_output(exc.stdout)
            stderr = self._decode_output(exc.stderr)
            return self._result(
                returncode=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                error=f"Python execution timed out after {self.timeout} seconds",
                elapsed_seconds=elapsed_seconds,
            )
        except Exception as exc:
            return self._error(str(exc))

        elapsed_seconds = time.perf_counter() - started_at

        return self._result(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            error=None,
            elapsed_seconds=elapsed_seconds,
        )

    def _result(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
        error: str | None,
        elapsed_seconds: float,
    ) -> dict[str, object]:
        combined_parts = [part.rstrip("\n") for part in (stdout, stderr) if part]
        combined_output = "\n".join(part for part in combined_parts if part)

        output_view = truncate_tail(combined_output)
        output_view_dict = output_view.to_dict()
        output_view_dict.pop("content")

        payload: dict[str, object] = {
            "ok": returncode == 0 and not timed_out,
            "python_executable": current_python_executable(),
            "returncode": returncode,
            "timeout": self.timeout,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed_seconds,
            "output": output_view.content.rstrip("\n"),
            "outputTruncationDetails": output_view_dict,
        }

        if error is not None:
            payload["error"] = error
        elif returncode != 0:
            payload["error"] = f"Python exited with status {returncode}"

        return payload

    @staticmethod
    def _decode_output(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output
        return output.decode("utf-8", errors="replace")
