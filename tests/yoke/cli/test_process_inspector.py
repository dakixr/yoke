"""Tests for the live command-process inspector."""

# ruff: noqa: D103, S101

import os
import shlex
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from prompt_toolkit.formatted_text import HTML

from yoke.agent.loop import RuntimeAgent
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.ai.providers.base import Provider
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.process_inspector import ProcessInspectorState
from yoke.cli.interactive.process_inspector.app import (
    PROCESS_INSPECTOR_MIN_REDRAW_INTERVAL,
)
from yoke.cli.interactive.process_inspector.app import ProcessSnapshotCache
from yoke.cli.interactive.process_inspector.app import (
    open_live_process_inspector,
)
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


def test_process_snapshot_cache_coalesces_output_notifications() -> None:
    class SnapshotManager:
        def __init__(self) -> None:
            self.calls = 0

        def snapshots(self) -> list[CommandProcessSnapshot]:
            self.calls += 1
            return [_snapshot(100, output="x" * (1024 * 1024))]

    manager = SnapshotManager()
    cache = ProcessSnapshotCache(
        cast("CommandProcessManager", manager),
        refresh_interval=60,
    )
    initial = cache.snapshots()

    for _ in range(256):
        cache.mark_dirty()
    refreshed = cache.snapshots()
    assert cache.snapshots() is refreshed
    assert initial is not refreshed
    assert manager.calls == 2


def test_process_detail_output_wrapping_is_cached(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.process_inspector.render.terminal_size",
        lambda: (120, 32),
    )
    from yoke.cli.interactive.process_inspector import render as process_render

    wrapped_lengths: list[int] = []
    original_wrap = process_render.textwrap.wrap

    def tracking_wrap(text: str, *args, **kwargs) -> list[str]:
        wrapped_lengths.append(len(text))
        return original_wrap(text, *args, **kwargs)

    monkeypatch.setattr(process_render.textwrap, "wrap", tracking_wrap)
    output = "x" * 10_000
    process = _snapshot(200, output=output)
    state = ProcessInspectorState([process])

    render_view_html(state, [process])
    assert len(output) in wrapped_lengths

    wrapped_lengths.clear()
    elapsed_update = replace(process, elapsed_seconds=process.elapsed_seconds + 1)
    render_view_html(state, [elapsed_update])
    assert len(output) not in wrapped_lengths

    wrapped_lengths.clear()
    output_update = replace(
        elapsed_update,
        output_tail=f"{output}y",
        original_output_bytes=process.original_output_bytes + 1,
        retained_output_bytes=process.retained_output_bytes + 1,
    )
    render_view_html(state, [output_update])
    assert len(output) + 1 in wrapped_lengths


def test_live_process_inspector_throttles_redraws(monkeypatch) -> None:
    import prompt_toolkit.application

    application_options: dict[str, object] = {}
    manager = None

    class FakeApplication:
        def __init__(self, **kwargs) -> None:
            application_options.update(kwargs)

        def invalidate(self) -> None:
            pass

        def run(self) -> None:
            assert manager is not None
            layout = cast(Any, application_options["layout"])
            control = layout.container.content
            control.text()
            control.text()
            assert manager.listener is not None
            manager.listener()
            control.text()
            control.text()

    class SnapshotManager:
        def __init__(self) -> None:
            self.calls = 0
            self.listener = None

        def snapshots(self) -> list[CommandProcessSnapshot]:
            self.calls += 1
            return []

        def subscribe(self, listener) -> object:
            self.listener = listener
            return lambda: None

    manager = SnapshotManager()
    monkeypatch.setattr(prompt_toolkit.application, "Application", FakeApplication)
    open_live_process_inspector(cast("CommandProcessManager", manager))

    assert (
        application_options["min_redraw_interval"]
        == PROCESS_INSPECTOR_MIN_REDRAW_INTERVAL
    )
    assert manager.calls == 2


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
