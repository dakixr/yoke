"""HTTP projection and control over loaded command process managers."""

from __future__ import annotations

from datetime import UTC
import hashlib
from typing import TYPE_CHECKING

from yoke.http.errors import ApiError
from yoke.http.models.process import ProcessInfo
from yoke.http.models.process import ProcessListResponse
from yoke.http.models.process import ProcessOutputChunk
from yoke.http.models.process import ProcessOutputCursor
from yoke.http.models.process import ProcessOutputInfo
from yoke.http.models.process import ProcessOutputResponse
from yoke.http.models.process import ProcessResponse
from yoke.http.services.runtime_registry import SessionRuntimeRegistry

if TYPE_CHECKING:
    from yoke.agent.tools.command_process_manager import CommandProcessManager
    from yoke.agent.tools.command_process_types import CommandProcessSnapshot


class ProcessService:
    """Inspect runtime-retained process state without consuming tool output."""

    def __init__(self, registry: SessionRuntimeRegistry) -> None:
        self.registry = registry

    def list_processes(
        self,
        *,
        session_id: str | None,
        status: str | None,
        limit: int,
    ) -> ProcessListResponse:
        processes: list[ProcessInfo] = []
        for owner_session_id, runtime in self.registry.loaded_runtimes():
            if session_id is not None and owner_session_id != session_id:
                continue
            manager = runtime.process_manager()
            if manager is None:
                continue
            for snapshot in manager.snapshots():
                if status is not None and snapshot.status != status:
                    continue
                processes.append(_process_info(owner_session_id, snapshot))
        processes.sort(key=lambda item: item.started_at, reverse=True)
        return ProcessListResponse(data=processes[:limit])

    def process(self, process_id: str) -> ProcessResponse:
        session_id, manager, snapshot = self._resolve(process_id)
        return ProcessResponse(data=_process_info(session_id, snapshot))

    def output(
        self,
        process_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> ProcessOutputResponse:
        _, manager, snapshot = self._resolve(process_id)
        page = manager.output_chunks(
            snapshot.session_id,
            after_seq=after_seq,
            limit=limit,
        )
        return ProcessOutputResponse(
            data=[
                ProcessOutputChunk(seq=chunk.seq, text=chunk.text)
                for chunk in page.chunks
            ],
            cursor=ProcessOutputCursor(
                next=page.latest_seq,
                truncated_before=page.truncated_before_seq,
            ),
        )

    def write_stdin(self, process_id: str, text: str) -> ProcessResponse:
        if not text:
            raise ApiError(400, "empty_process_input", "stdin text cannot be empty.")
        session_id, manager, snapshot = self._resolve(process_id)
        if snapshot.status != "running":
            raise ApiError(409, "process_not_running", "Process is not running.")
        try:
            manager.write_input(snapshot.session_id, text)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ApiError(409, "process_input_failed", str(exc)) from exc
        return ProcessResponse(
            data=_process_info(session_id, manager.snapshot(snapshot.session_id))
        )

    def signal(self, process_id: str, signal_name: str) -> ProcessResponse:
        session_id, manager, snapshot = self._resolve(process_id)
        if snapshot.status != "running":
            raise ApiError(409, "process_not_running", "Process is not running.")
        try:
            if signal_name == "interrupt":
                manager.interrupt(snapshot.session_id)
            elif signal_name == "terminate":
                manager.terminate(snapshot.session_id)
            else:
                raise ApiError(400, "invalid_process_signal", "Unsupported signal.")
            updated = manager.snapshot(snapshot.session_id)
        except ValueError as exc:
            raise ApiError(404, "process_not_found", "Process was not found.") from exc
        return ProcessResponse(data=_process_info(session_id, updated))

    def _resolve(
        self,
        process_id: str,
    ) -> tuple[str, CommandProcessManager, CommandProcessSnapshot]:
        for session_id, runtime in self.registry.loaded_runtimes():
            manager = runtime.process_manager()
            if manager is None:
                continue
            for snapshot in manager.snapshots():
                if _process_id(session_id, snapshot) == process_id:
                    return session_id, manager, snapshot
        raise ApiError(404, "process_not_found", "Process was not found.")


def _process_id(session_id: str, snapshot: CommandProcessSnapshot) -> str:
    identity = f"{session_id}\0{snapshot.session_id}\0{snapshot.started_at.isoformat()}".encode()
    return "proc_" + hashlib.sha256(identity).hexdigest()[:24]


def _process_info(session_id: str, snapshot: CommandProcessSnapshot) -> ProcessInfo:
    return ProcessInfo(
        process_id=_process_id(session_id, snapshot),
        session_id=session_id,
        runtime_session_id=snapshot.session_id,
        pid=snapshot.pid,
        command=snapshot.command,
        cwd=str(snapshot.cwd),
        tty=snapshot.tty,
        status=snapshot.status,
        started_at=snapshot.started_at.astimezone(UTC).isoformat(),
        elapsed_ms=max(0, round(snapshot.elapsed_seconds * 1000)),
        exit_code=snapshot.exit_code,
        output=ProcessOutputInfo(
            tail=snapshot.output_tail,
            original_bytes=snapshot.original_output_bytes,
            retained_bytes=snapshot.retained_output_bytes,
            truncated=snapshot.original_output_bytes > snapshot.retained_output_bytes,
            latest_seq=snapshot.latest_output_seq,
        ),
    )
