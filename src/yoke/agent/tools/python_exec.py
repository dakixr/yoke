from __future__ import annotations

import os
import subprocess
import time

from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.command import _ProcessOutputReader
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
        env.setdefault("PYTHONUNBUFFERED", "1")
        python_executable = current_python_executable()

        try:
            process = subprocess.Popen(  # noqa: S603
                [python_executable, "-u", "-c", self.code],
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            output_reader = _ProcessOutputReader(process)
            output_reader.start()
            deadline = time.monotonic() + self.timeout
            while process.poll() is None:
                output_reader.emit_pending(self._emit_output_chunk)
                if self._is_cancel_requested():
                    self._terminate_process(process)
                    stdout, stderr = output_reader.finish(self._emit_output_chunk)
                    return self._result(
                        returncode=process.returncode
                        if process.returncode is not None
                        else -1,
                        stdout=stdout,
                        stderr=stderr,
                        timed_out=False,
                        error="Python execution cancelled",
                        elapsed_seconds=time.perf_counter() - started_at,
                    )
                if time.monotonic() >= deadline:
                    self._terminate_process(process)
                    stdout, stderr = output_reader.finish(self._emit_output_chunk)
                    elapsed_seconds = time.perf_counter() - started_at
                    return self._result(
                        returncode=-1,
                        stdout=stdout,
                        stderr=stderr,
                        timed_out=True,
                        error=f"Python execution timed out after {self.timeout} seconds",
                        elapsed_seconds=elapsed_seconds,
                    )
                time.sleep(0.05)
            stdout, stderr = output_reader.finish(self._emit_output_chunk)
        except Exception as exc:
            return self._error(str(exc))

        elapsed_seconds = time.perf_counter() - started_at

        return self._result(
            returncode=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            error=None,
            elapsed_seconds=elapsed_seconds,
        )

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _emit_output_chunk(self, stream: str, text: str) -> None:
        if text:
            self._emit_tool_event(
                "tool_execution_output_delta",
                {"stream": stream, "text": text},
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
