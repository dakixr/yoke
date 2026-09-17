"""Deterministic output paging, retention, and provider projection contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_support.admission import allocate_session_id
from yoke.agent.tools.command_process_support import output as output_module
from yoke.agent.tools.command_process_support.output import (
    CompletedCommandProcess,
    RetainedProcessOutput,
    command_decoder,
)
from yoke.agent.tools.command_process_types import CommandProcessSnapshot
from yoke.agent.tools.processes.cursor import decode_cursor
from yoke.agent.tools.processes.cursor import encode_cursor
from yoke.agent.tools.processes.cursor import ProcessPosition
from yoke.agent.tools.processes.models import ProcessCursor, ProcessReadRequest
from yoke.agent.tools.processes.observation import page, read_processes
from yoke.agent.tool_result_projection_command import project_command_result


def retain(manager, root: Path, output: RetainedProcessOutput, session: int = 1234):
    snapshot = CommandProcessSnapshot(
        session_id=session,
        pid=session,
        command="test",
        cwd=root,
        tty=False,
        status="exited",
        started_at=datetime.now(timezone.utc),
        elapsed_seconds=0,
        exit_code=0,
        output_tail=output.tail(),
        original_output_bytes=output.original_bytes,
        retained_output_bytes=output.retained_bytes,
        latest_output_seq=output.latest_seq,
        truncated_before_seq=output.truncated_before_seq,
    )
    manager._completed.append(CompletedCommandProcess(snapshot, output.freeze()))


def test_unicode_pages_are_repeatable_and_do_not_advance_past_unseen_bytes(tmp_path):
    output = RetainedProcessOutput()
    expected = "a🌍é\n" * 70
    output.append(expected.encode())
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        cursor = ProcessPosition(session_id=1234)
        text = ""
        for _ in range(1000):
            first = page(manager, cursor, 7)
            assert first == page(manager, cursor, 7)
            assert len(first["output"].encode()) <= 7
            assert not first["gap"]
            text += first["output"]
            cursor = decode_cursor(1234, first["cursor"])
            if not first["has_more_output"]:
                break
        assert text == expected
        assert cursor.after_seq == 1 and cursor.offset == 0
        assert not page(manager, cursor, 7)["has_more_output"]
    finally:
        manager.close()


def test_one_response_can_cross_more_than_one_hundred_output_records(tmp_path):
    output = RetainedProcessOutput()
    for _ in range(500):
        output.append(b"x\n")
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        result = page(manager, ProcessPosition(session_id=1234), 64000)
        assert result["output"] == "x\n" * 500
        assert decode_cursor(1234, result["cursor"]).after_seq == 500
        assert not result["has_more_output"]
    finally:
        manager.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"after_seq": 2},
        {"after_seq": 1, "offset": 1},
        {"offset": 999},
        {"offset": 2},
        {"offset": 6},
    ],
)
def test_invalid_future_or_non_utf8_boundary_cursor_fails(tmp_path, fields):
    output = RetainedProcessOutput()
    output.append("a🌍z".encode())
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        with pytest.raises(ValueError):
            page(manager, ProcessPosition(session_id=1234, **fields), 1024)
    finally:
        manager.close()


def test_gap_is_reported_once_and_does_not_hide_remaining_output(tmp_path, monkeypatch):
    monkeypatch.setattr(output_module, "MAX_RETAINED_OUTPUT_BYTES", 8)
    output = RetainedProcessOutput()
    output.append(b"lost-one")
    output.append(b"kept-two")
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        first = page(manager, ProcessPosition(session_id=1234, offset=3), 4)
        assert first["gap"]
        assert first["output"] == "kept"
        assert first["has_more_output"]
        second = page(manager, decode_cursor(1234, first["cursor"]), 4)
        assert not second["gap"] and second["output"] == "-two"
        assert not second["has_more_output"]
    finally:
        manager.close()


def test_evicted_entire_history_advances_only_with_explicit_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(output_module, "MAX_RETAINED_OUTPUT_BYTES", 4)
    output = RetainedProcessOutput()
    output.append(b"all-of-this-is-gone")
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        first = page(manager, ProcessPosition(session_id=1234), 1024)
        assert first["gap"] and first["output"] == ""
        assert decode_cursor(1234, first["cursor"]).after_seq == output.latest_seq
        assert not page(manager, decode_cursor(1234, first["cursor"]), 1024)["gap"]
    finally:
        manager.close()


def test_newline_representation_does_not_change_after_retention_eviction(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(output_module, "MAX_RETAINED_OUTPUT_BYTES", 12)
    output = RetainedProcessOutput()
    output.append(b"first\r")
    output.append(b"\nsecond")
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        first = page(manager, ProcessPosition(session_id=1234), 3)
        assert first["gap"] and first["output"] == "\nse"
        second = page(manager, decode_cursor(1234, first["cursor"]), 4)
        assert second["output"] == "cond"
        assert not second["has_more_output"]
    finally:
        manager.close()


def test_total_batch_budget_is_shared_without_starving_later_sessions(tmp_path):
    output = RetainedProcessOutput()
    output.append(("🌍" * 400).encode())
    manager = CommandProcessManager()
    try:
        for session in range(1, 17):
            retain(manager, tmp_path, output, session)
        result = read_processes(
            manager,
            ProcessReadRequest(
                sessions=[ProcessCursor(session_id=i) for i in range(1, 17)],
                max_bytes=1024,
            ),
        )
        assert result["reason"] == "completed"
        assert sum(len(item["output"].encode()) for item in result["items"]) == 1024
        assert all(
            item["has_more_output"] and len(item["output"].encode()) == 64
            for item in result["items"]
        )
    finally:
        manager.close()


def test_completed_result_projection_keeps_cursor_and_unread_output(tmp_path):
    output = RetainedProcessOutput()
    output.append(b"abcdef")
    manager = CommandProcessManager()
    try:
        retain(manager, tmp_path, output)
        result = {
            **page(manager, ProcessPosition(session_id=1234), 4),
            "reason": "completed",
        }
        projection = project_command_result(result)
        assert projection is not None
        projected = json.loads(projection)
        assert projected["cursor"] == result["cursor"]
        assert projected["has_more_output"] and not projected["running"]
        assert projected["exit_code"] == 0 and projected["session_id"] == 1234
        assert projected["next_tool"] == "process_read"
        assert project_command_result({**result, "custom_extension": True}) is None
    finally:
        manager.close()


def test_public_cursor_is_opaque_versioned_and_session_bound() -> None:
    position = ProcessPosition(session_id=1234, after_seq=57, offset=11)
    cursor = encode_cursor(position)

    assert cursor.startswith("pc1_") and len(cursor) == 36
    assert "57" not in cursor and "11" not in cursor
    assert decode_cursor(1234, cursor) == position
    with pytest.raises(ValueError, match="different session"):
        decode_cursor(1235, cursor)


def test_stream_decoder_retains_split_utf8_and_legacy_bytes():
    decoder = command_decoder()
    text = "a🌍é\n"
    decoded = "".join(decoder.decode(bytes([byte])) for byte in text.encode())
    decoded += decoder.decode(b"", final=True)
    assert decoded == text
    legacy = command_decoder()
    assert legacy.decode(b"caf\xe9", final=True) == "café"


def test_process_handles_are_not_recycled_after_history_eviction(tmp_path):
    manager = CommandProcessManager()
    try:
        first = allocate_session_id(manager)
        # Nothing retains this handle. A later allocation must still be distinct.
        later = [allocate_session_id(manager) for _ in range(1000)]
        assert first not in later
        assert len(set(later)) == len(later)
        with pytest.raises(ValueError, match="Unknown command session"):
            manager.snapshot(first)
    finally:
        manager.close()
