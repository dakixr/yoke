"""Tests for the live command-process inspector."""

# ruff: noqa: D103, S101

import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from prompt_toolkit.formatted_text import HTML

from yoke.agent.loop import RuntimeAgent
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.ai.providers.base import Provider
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.process_inspector import ProcessInspectorState
from yoke.cli.interactive.process_inspector.render import (
    render_view_html,
)
from yoke.cli.interactive.process_commands import _safe_text
from yoke.cli.interactive.process_commands import print_process_table
from yoke.cli.render import build_console
from yoke.cli.runtime import create_active_session

from .support import CaptureStream
from .support import FakeAgent
from .support import FakeProvider


def _snapshot(
    session_id: int,
    *,
    status: Literal["running", "exited", "failed"] = "running",
    output: str = "",
) -> CommandProcessSnapshot:
    return CommandProcessSnapshot(
        session_id=session_id,
        pid=session_id + 1_000,
        command=f"python worker_{session_id}.py",
        cwd=Path("C:/workspace"),
        tty=False,
        status=status,
        started_at=datetime.now().astimezone(),
        elapsed_seconds=2.5,
        exit_code=None if status == "running" else 0,
        output_tail=output,
        original_output_bytes=len(output),
        retained_output_bytes=len(output),
    )


def test_process_inspector_renders_process_metadata_and_safe_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.process_inspector.render.terminal_size",
        lambda: (120, 32),
    )
    processes = [
        _snapshot(1234),
        _snapshot(
            5678,
            status="exited",
            output="\x00\x1b[31mdone\x1b[0m\ud800\n<ansired>literal",
        ),
    ]
    state = ProcessInspectorState(processes)

    formatted = HTML(render_view_html(state, processes)).__pt_formatted_text__()
    text = "".join(fragment[1] for fragment in formatted)

    assert state.selected_index == 1
    assert "Command session 5678" in text
    assert "OS PID" in text
    assert "␀␛[31mdone␛[0m�" in text
    assert "<ansired>literal" in text


def test_ps_slash_command_opens_prompt_toolkit_inspector(
    tmp_path: Path,
) -> None:
    active_session = create_active_session(CLIArgs(root=str(tmp_path)), root=tmp_path)
    opened: list[bool] = []

    handled, messages, updated_session = handle_slash_command(
        "/ps",
        agent=FakeAgent(),
        active_session=active_session,
        messages=[],
        console=build_console(CaptureStream()),
        on_process_inspector=lambda: opened.append(True),
    )

    assert handled is True
    assert messages == []
    assert updated_session is active_session
    assert opened == [True]


def test_basic_process_table_escapes_c1_terminal_controls() -> None:
    assert _safe_text("before\x9b31m\x9dafter") == "before\\x9b31m\\x9dafter"


def test_basic_process_table_lists_runtime_owned_processes(
    tmp_path: Path,
) -> None:
    command = (
        f'& "{sys.executable}" -c "import time; time.sleep(5)"'
        if os.name == "nt"
        else f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(5)'"
    )
    agent = RuntimeAgent(provider=cast(Provider, FakeProvider()), tools=[])
    try:
        result = agent.command_process_manager.exec_command(
            command=command,
            cwd=tmp_path,
            tty=False,
            yield_time_ms=250,
            shell=None,
            login=True,
            cancel_requested=None,
        )
        assert result.session_id is not None

        stream = CaptureStream()
        print_process_table(build_console(stream), agent)
        output = stream.getvalue()

        assert "Command Processes" in output
        assert str(result.session_id) in output
        assert "running" in output
        assert "time.sleep" in output
    finally:
        agent.close()
