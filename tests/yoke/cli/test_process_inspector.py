"""Tests for the live command-process inspector."""

# ruff: noqa: D103,S101

import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from prompt_toolkit.formatted_text import HTML

from yoke.agent.tools import CommandProcessManager
from yoke.agent.tools import CommandProcessSnapshot
from yoke.cli.interactive.process_inspector import ProcessInspectorState
from yoke.cli.interactive.process_inspector.render import render_view_html


def snapshot(
    session_id: int,
    *,
    status: Literal["running", "exited", "failed"] = "running",
    output: str = "",
) -> CommandProcessSnapshot:
    return CommandProcessSnapshot(
        session_id=session_id,
        pid=session_id + 1000,
        command=f"python worker_{session_id}.py",
        cwd=Path("/workspace"),
        tty=False,
        status=status,
        started_at=datetime.now().astimezone(),
        elapsed_seconds=2.5,
        exit_code=None if status == "running" else 0,
        output_tail=output,
        original_output_bytes=len(output),
        retained_output_bytes=len(output),
    )


def test_process_inspector_renders_safe_process_output(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.process_inspector.render.terminal_size",
        lambda: (120, 32),
    )
    processes = [
        snapshot(1234),
        snapshot(5678, status="exited", output="\x00\x1b[31mdone\x1b[0m\n<tag>"),
    ]
    state = ProcessInspectorState(processes)

    formatted = HTML(render_view_html(state, processes)).__pt_formatted_text__()
    text = "".join(fragment[1] for fragment in formatted)

    assert state.selected_index == 1
    assert "Command session 5678" in text
    assert "␀␛[31mdone␛[0m" in text
    assert "<tag>" in text


def test_command_manager_retains_completed_snapshot_and_output(tmp_path: Path) -> None:
    manager = CommandProcessManager()
    command = f"{shlex.quote(sys.executable)} -c 'print(\"done\")'"
    result = manager.exec_command(
        command=command,
        cwd=tmp_path,
        tty=False,
        yield_time_ms=30_000,
        shell=None,
        login=True,
        tool_event=None,
        cancel_requested=None,
    )

    assert result.session_id is None
    retained = manager.snapshots()
    assert len(retained) == 1
    assert retained[0].status == "exited"
    assert retained[0].exit_code == 0
    assert retained[0].output_tail == "done\n"
