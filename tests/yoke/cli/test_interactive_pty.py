"""One real terminal rendering smoke; command behavior is covered in unit tests."""

from __future__ import annotations

import asyncio
from contextlib import closing
import os
import select
import threading
from typing import cast

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.output.vt100 import Vt100_Output

from yoke._version import __version__
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.prompt.app import run_prompt_toolkit_cli

from .terminal_daemon_support import terminal as terminal


def test_interactive_pty_renders_banner_and_exits(terminal, tmp_path, monkeypatch):
    master, slave = os.openpty()
    rendered = bytearray()
    ready = threading.Event()
    application_ready = threading.Event()
    stopped = threading.Event()

    def on_created(value: object) -> None:
        session = cast(PromptSession[str], value)

        async def deadline() -> None:
            await asyncio.sleep(5)
            session.app.exit(exception=AssertionError("PTY smoke did not exit"))

        def started() -> None:
            application_ready.set()
            session.app.create_background_task(deadline())

        session.app.pre_run_callables.append(started)

    def drive():
        sent = False
        while not stopped.is_set() or select.select([master], [], [], 0)[0]:
            if select.select([master], [], [], 0.05)[0]:
                chunk = os.read(master, 65536)
                if not chunk:
                    return
                rendered.extend(chunk)
            # The banner can render before the application signals readiness;
            # check on every tick, not only when new output arrives.
            if (
                application_ready.is_set()
                and __version__.encode() in rendered
                and not sent
            ):
                ready.set()
                os.write(master, b"exit\r")
                sent = True

    try:
        with (
            os.fdopen(os.dup(slave), "r", encoding="utf-8") as stdin,
            os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1) as stdout,
            closing(create_input(stdin=stdin)) as input_device,
        ):
            monkeypatch.setattr("sys.stdout", stdout)
            backend = terminal.backend("pty-smoke", root=tmp_path)
            output = Vt100_Output(
                stdout, lambda: Size(rows=24, columns=100), enable_cpr=False
            )
            worker = threading.Thread(target=drive, daemon=True)
            worker.start()
            try:
                result = run_prompt_toolkit_cli(
                    CLIArgs(root=str(tmp_path)),
                    backend,
                    [],
                    active_session=backend.session,
                    pt_input=input_device,
                    pt_output=output,
                    on_app_created=on_created,
                )
            finally:
                stopped.set()
                worker.join(timeout=2)
            assert result == 0
            assert ready.is_set()
            assert not worker.is_alive()
            assert __version__.encode() in rendered
            assert b"\x1b[?2004h" in rendered
            assert b"\x1b[?2004l" in rendered
    finally:
        os.close(master)
        os.close(slave)
