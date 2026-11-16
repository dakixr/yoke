"""Convert public daemon inspector DTOs into the terminal's read-only values."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from yoke.agent.observability import (
    ToolTraceContext,
    ToolTraceEntry,
    ToolTraceOutputChunk,
)
from yoke.agent.tools.command_process.types import CommandProcessSnapshot
from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.http.models.agent_run import AgentRunListResponse
from yoke.http.models.process import ProcessListResponse
from yoke.http.models.tool_trace import (
    ToolCallInfo,
    ToolCallListResponse,
    ToolCallResponse,
    ToolOutputResponse,
)


class DaemonInspection:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def snapshots(self) -> list[CommandProcessSnapshot]:
        data = ProcessListResponse.model_validate(
            self.connection.request(
                "GET", "process", params={"sessionID": self.connection.session_id}
            )
        ).data
        return [
            CommandProcessSnapshot(
                session_id=item.runtime_session_id,
                pid=item.pid,
                command=item.command,
                cwd=Path(item.cwd),
                tty=item.tty,
                status=item.status,
                started_at=datetime.fromisoformat(item.started_at),
                elapsed_seconds=item.elapsed_ms / 1000,
                exit_code=item.exit_code,
                output_tail=item.output.tail,
                original_output_bytes=item.output.original_bytes,
                retained_output_bytes=item.output.retained_bytes,
                latest_output_seq=item.output.latest_seq,
            )
            for item in data
        ]

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        del changed
        return lambda: None

    def agent_runs(self) -> list[dict[str, object]]:
        data = AgentRunListResponse.model_validate(
            self.connection.request(
                "GET", "agent-run", params={"sessionID": self.connection.session_id}
            )
        ).data
        return [item.model_dump() for item in data]


class DaemonToolInspection:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def snapshots(self) -> list[ToolTraceEntry]:
        values: list[ToolTraceEntry] = []
        cursor = None
        while True:
            params: dict[str, str | int] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = ToolCallListResponse.model_validate(
                self.connection.session_request("GET", "/tool-call", params=params)
            )
            values.extend(self.present(item) for item in page.data)
            cursor = page.cursor.next
            if not cursor:
                return values

    @staticmethod
    def present(item: ToolCallInfo) -> ToolTraceEntry:
        duration = item.time.duration_ms
        # TraceEntry treats a falsey end time as still running. Keep the
        # synthetic monotonic anchor positive even for a zero-duration call.
        return ToolTraceEntry(
            tool_call_id=item.id,
            tool_name=item.tool_name,
            raw_arguments=item.arguments.raw,
            executed_arguments=item.arguments.executed,
            result=item.result,
            provider_result_projection=item.result_projection,
            iteration=item.iteration,
            turn_id=item.turn_id,
            started_at=1.0 if duration is not None else None,
            ended_at=1.0 + duration / 1000 if duration is not None else None,
            started_wall_at=item.time.started,
            ended_wall_at=item.time.ended,
            status=item.status,
            context=[
                ToolTraceContext(role=value.role, text=value.text)
                for value in item.context
            ],
            after_context=[
                ToolTraceContext(role=value.role, text=value.text)
                for value in item.after_context
            ],
        )

    def revision(self) -> tuple[int, int, int]:
        values = self.snapshots()
        return (
            len(values),
            sum(item.status in {"ok", "failed", "cancelled"} for item in values),
            0,
        )

    def call(self, call_id: str) -> ToolTraceEntry | None:
        try:
            item = ToolCallResponse.model_validate(
                self.connection.session_request("GET", f"/tool-call/{call_id}")
            ).data
        except DaemonRequestError as exc:
            if exc.status != 404:
                raise
            return None
        result = self.present(item)
        result.output_chunks = self.output(call_id)
        return result

    def output(self, call_id: str, *, after_seq: int = 0) -> list[ToolTraceOutputChunk]:
        page = ToolOutputResponse.model_validate(
            self.connection.session_request(
                "GET", f"/tool-call/{call_id}/output", params={"afterSeq": after_seq}
            )
        )
        return [
            ToolTraceOutputChunk(stream=value.stream, text=value.text, seq=value.seq)
            for value in page.data
        ]

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        del changed
        return lambda: None
