"""Tool for executing shell commands in the workspace root."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

from pydantic import AliasChoices
from pydantic import Field

from yoke.agent.tools.python.env import current_python_executable
from yoke.agent.tools.python.env import prepare_python_env
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


def decode_command_output_chunk(raw: bytes) -> str:
    """Decode a streamed command output chunk without normalizing line endings."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
                stdin=subprocess.PIPE if os.name != "nt" else None,
                env=env,
                start_new_session=os.name != "nt",
            )
            output_reader = _ProcessOutputReader(process)
            output_reader.start()
            if os.name != "nt" and process.stdin is not None:
                process.stdin.write(self.command.encode("utf-8"))
                process.stdin.close()
                process.stdin = None
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                output_reader.emit_pending(self._emit_output_chunk)
                if self._is_cancel_requested():
                    return self._cancel_process(
                        process,
                        output_reader=output_reader,
                        elapsed_seconds=time.perf_counter() - started_at,
                    )
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return self._timeout_process(
                        process,
                        output_reader=output_reader,
                        elapsed_seconds=time.perf_counter() - started_at,
                    )
                if process.poll() is None:
                    time.sleep(0.05 if remaining is None else min(0.05, remaining))
                    continue
                stdout, stderr = output_reader.finish(self._emit_output_chunk)
                elapsed_seconds = time.perf_counter() - started_at
                return self._build_result(
                    returncode=process.returncode or 0,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                    elapsed_seconds=elapsed_seconds,
                )
        except Exception as exc:
            return self._error(str(exc), command=self.command, timeout=self.timeout)

    def _cancel_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        output_reader: _ProcessOutputReader | None = None,
        elapsed_seconds: float | None = None,
    ) -> dict[str, object]:
        if elapsed_seconds is None:
            elapsed_seconds = 0.0
        self._terminate_process(process)
        stdout, stderr = _finish_process_output(
            process,
            output_reader,
            self._emit_output_chunk,
        )
        result = self._build_result(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
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
        self,
        process: subprocess.Popen[bytes],
        *,
        output_reader: _ProcessOutputReader | None = None,
        elapsed_seconds: float | None = None,
    ) -> dict[str, object]:
        if elapsed_seconds is None:
            elapsed_seconds = float(self.timeout or 0)
        self._terminate_process(process)
        stdout, stderr = _finish_process_output(
            process,
            output_reader,
            self._emit_output_chunk,
        )
        result = self._build_result(
            returncode=-1,
            stdout=stdout,
            stderr=stderr,
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

    def _emit_output_chunk(self, stream: str, text: str) -> None:
        if text:
            self._emit_tool_event(
                "tool_execution_output_delta",
                {"stream": stream, "text": text},
            )


class _ProcessOutputReader:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._process.stdout is not None:
            self._threads.append(
                threading.Thread(
                    target=self._read_stream,
                    args=("stdout", self._process.stdout),
                    daemon=True,
                )
            )
        if self._process.stderr is not None:
            self._threads.append(
                threading.Thread(
                    target=self._read_stream,
                    args=("stderr", self._process.stderr),
                    daemon=True,
                )
            )
        for thread in self._threads:
            thread.start()

    def emit_pending(self, emit_chunk) -> None:
        while True:
            try:
                stream, raw = self._queue.get_nowait()
            except queue.Empty:
                return
            text = decode_command_output_chunk(raw)
            if stream == "stderr":
                self._stderr_parts.append(text)
            else:
                self._stdout_parts.append(text)
            emit_chunk(stream, text)

    def finish(self, emit_chunk) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout=1)
        self.emit_pending(emit_chunk)
        return _normalize_output("".join(self._stdout_parts)), _normalize_output(
            "".join(self._stderr_parts)
        )

    def _read_stream(self, stream: str, pipe) -> None:
        while True:
            raw = pipe.readline()
            if not raw:
                remainder = pipe.read()
                if remainder:
                    self._queue.put((stream, remainder))
                return
            self._queue.put((stream, raw))


def _finish_process_output(
    process: subprocess.Popen[bytes],
    output_reader: _ProcessOutputReader | None,
    emit_chunk,
) -> tuple[str, str]:
    if output_reader is not None:
        return output_reader.finish(emit_chunk)
    stdout_raw, stderr_raw = process.communicate()
    return decode_command_output(stdout_raw), decode_command_output(stderr_raw)


def _normalize_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
