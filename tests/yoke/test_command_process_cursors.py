"""Regression tests for command-process output retention and cursors."""

from __future__ import annotations

from pathlib import Path
import subprocess
from collections.abc import Iterator
from typing import cast

import pytest

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_support.output import RetainedProcessOutput
from yoke.agent.tools.command_process_types import MAX_RETAINED_OUTPUT_BYTES
from yoke.http.services.process_service import ProcessService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.mcp_server.execution.processes import ProcessCursor
from yoke.mcp_server.execution.processes import page as mcp_process_page


class _FinishedProcess:
    pid = 2_000_000_001
    stdin = None
    stdout = None
    stderr = None

    def poll(self) -> int:
        return 0


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[CommandProcessManager]:
    monkeypatch.setattr(
        "yoke.agent.tools.command_process.terminate_owned_process_tree",
        lambda *_args: None,
    )
    owned = CommandProcessManager()
    try:
        yield owned
    finally:
        owned.close()


def _managed(manager: CommandProcessManager, session_id: int) -> _ManagedCommandProcess:
    process = _ManagedCommandProcess(
        session_id=session_id,
        command="TEST-OWNED deterministic output",
        cwd=Path.cwd(),
        process=cast(subprocess.Popen[bytes], _FinishedProcess()),
        tty=False,
        master_fd=None,
        on_change=lambda: None,
    )
    manager._processes[session_id] = process
    return process


def test_completed_transition_preserves_exact_unread_chunks(
    manager: CommandProcessManager,
) -> None:
    process = _managed(manager, 2_001)
    process._append_output(b"A")
    first = manager.output_chunks(2_001, after_seq=0, limit=100)
    assert [(chunk.seq, chunk.text) for chunk in first.chunks] == [(1, "A")]

    process._append_output(b"B")
    manager._complete(2_001)
    second = manager.output_chunks(2_001, after_seq=1, limit=100)

    assert [(chunk.seq, chunk.text) for chunk in second.chunks] == [(2, "B")]
    assert second.latest_seq == 2
    assert second.truncated_before_seq == 0


def test_completed_ring_pages_more_than_one_hundred_chunks_without_duplicates(
    manager: CommandProcessManager,
) -> None:
    process = _managed(manager, 2_002)
    for index in range(137):
        process._append_output(f"{index},".encode())
    manager._complete(2_002)

    observed: list[tuple[int, str]] = []
    after_seq = 0
    while True:
        output = manager.output_chunks(2_002, after_seq=after_seq, limit=17)
        if not output.chunks:
            break
        observed.extend((chunk.seq, chunk.text) for chunk in output.chunks)
        after_seq = output.chunks[-1].seq

    assert observed == [(index + 1, f"{index},") for index in range(137)]
    assert output.latest_seq == 137
    assert output.truncated_before_seq == 0


def test_crlf_split_across_chunks_and_consumption_has_one_newline() -> None:
    output = RetainedProcessOutput()
    output.append(b"left\r")
    first, first_bytes = output.consume()
    output.append(b"\nright\r\ncp1252:\x96\r")
    second, second_bytes = output.consume()
    output.append(b"\nend")

    assert first == "left\n"
    assert first_bytes == 5
    assert second == "right\ncp1252:\N{EN DASH}\n"
    assert second_bytes == 17
    assert output.tail() == "left\nright\ncp1252:\N{EN DASH}\nend"
    assert [chunk.text for chunk in output.page(after_seq=1, limit=10).chunks] == [
        "right\ncp1252:\N{EN DASH}\n",
        "end",
    ]


def test_crlf_boundary_does_not_remove_lf_when_preceding_chunk_was_evicted() -> None:
    output = RetainedProcessOutput()
    output.append(b"x" * (MAX_RETAINED_OUTPUT_BYTES - 1) + b"\r")
    output.append(b"\n")

    page = output.page(after_seq=0, limit=10)
    consumed, original_bytes = output.consume()

    assert page.truncated_before_seq == 1
    assert [(chunk.seq, chunk.text) for chunk in page.chunks] == [(2, "\n")]
    assert output.tail() == "\n"
    assert consumed == "\n"
    assert original_bytes == MAX_RETAINED_OUTPUT_BYTES + 1


def test_mcp_cursor_crosses_completion_without_replaying_output(
    manager: CommandProcessManager,
) -> None:
    process = _managed(manager, 2_003)
    process._append_output(b"first")
    first = mcp_process_page(manager, ProcessCursor(session_id=2_003), 32_000)
    process._append_output(b"second")
    manager._complete(2_003)

    second = mcp_process_page(
        manager,
        ProcessCursor.model_validate(first["next_cursor"]),
        32_000,
    )

    assert first["output"] == "first"
    assert second["output"] == "second"
    assert second["gap"] is False
    assert second["status"] == "exited"


def test_http_cursor_crosses_completion_without_replaying_output(
    manager: CommandProcessManager,
) -> None:
    class Runtime:
        def process_manager(self) -> CommandProcessManager:
            return manager

    class Registry:
        def loaded_runtimes(self):
            return [("owner", Runtime())]

    process = _managed(manager, 2_004)
    process._append_output(b"first")
    service = ProcessService(cast(SessionRuntimeRegistry, Registry()))
    process_id = (
        service.list_processes(session_id="owner", status=None, limit=10)
        .data[0]
        .process_id
    )
    first = service.output(process_id, after_seq=0, limit=100)
    process._append_output(b"second")
    manager._complete(2_004)

    second = service.output(process_id, after_seq=first.cursor.next, limit=100)

    assert [chunk.text for chunk in first.data] == ["first"]
    assert [chunk.text for chunk in second.data] == ["second"]
    assert second.cursor.truncated_before == 0
