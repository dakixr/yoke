"""Exercise the real editor, daemon queue promotion, spinner and stop key."""

from __future__ import annotations

import asyncio
import os
import select
import threading
from contextlib import closing
from pathlib import Path
from typing import cast

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.output.vt100 import Vt100_Output

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider, ProviderCancelledError
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.prompt.app import run_prompt_toolkit_cli

from .terminal_daemon_support import terminal as terminal


class PtyProvider(Provider):
    def __init__(self):
        self.first = threading.Event()
        self.second = threading.Event()
        self.release_first = threading.Event()

    def complete(self, messages, tools):
        raise AssertionError("Expected cancellable provider")

    def complete_with_cancel(self, messages, tools, *, cancel_requested):
        prompt = next(
            message.text_content()
            for message in reversed(messages)
            if message.role == "user"
        )
        entered = self.first if prompt == "first" else self.second
        entered.set()
        while not (prompt == "first" and self.release_first.wait(0.001)):
            if cancel_requested():
                raise ProviderCancelledError()
            if prompt != "first":
                threading.Event().wait(0.001)
        return Message.assistant("first done")


def test_live_pty_idle_queue_promotion_spinner_and_escape_stop(
    terminal, tmp_path, monkeypatch
):
    monkeypatch.setenv("TERM", "xterm-256color")
    provider = PtyProvider()
    terminal.factory = lambda record: RuntimeAgent(
        provider, [], tool_root=Path(record.root)
    )
    backend = terminal.backend("pty-lifecycle", root=tmp_path)
    backend.connection.session_request("PATCH", json={"title": "fixed"})
    master, slave = os.openpty()
    rendered = bytearray()
    ready, stopped, completed = (threading.Event() for _ in range(3))
    errors = []
    toolbars = []
    from yoke.cli.interactive.prompt import rendering

    original = rendering.build_prompt_toolbar

    def toolbar_factory(**kwargs):
        toolbar = original(**kwargs)

        def get():
            text = "".join(text for _, text in toolbar())
            toolbars.append((backend.view_store.view, text))
            return toolbar()

        return get

    monkeypatch.setattr(rendering, "build_prompt_toolbar", toolbar_factory)

    def on_created(value):
        session = cast(PromptSession[str], value)

        async def deadline():
            await asyncio.sleep(12)
            if not completed.is_set():
                session.app.exit(
                    exception=AssertionError(
                        f"PTY lifecycle deadline; last view={backend.view_store.view}; toolbar={toolbars[-1:]}; errors={errors}"
                    )
                )

        def started():
            ready.set()
            session.app.create_background_task(deadline())

        session.app.pre_run_callables.append(started)

    def drive():
        stage = 0
        while not stopped.is_set():
            if select.select([master], [], [], 0.01)[0]:
                chunk = os.read(master, 65536)
                rendered.extend(chunk)
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
            try:
                view = backend.view_store.view
                if stage == 0 and ready.is_set():
                    os.write(master, b"first\r")
                    stage = 1
                elif stage == 1 and provider.first.is_set() and view.running:
                    assert not view.queue
                    os.write(master, b"second\t")
                    stage = 2
                elif (
                    stage == 2
                    and len(view.queue) == 1
                    and any(
                        "queued" in text and "second" in text for _, text in toolbars
                    )
                ):
                    assert view.queue[0].prompt == "second"
                    provider.release_first.set()
                    stage = 3
                elif (
                    stage == 3
                    and provider.second.is_set()
                    and view.running
                    and view.running.prompt == "second"
                ):
                    # Wait until the actual toolbar has drawn this daemon-promoted turn.
                    if not any(
                        v.running
                        and v.running.prompt == "second"
                        and any(frame in text for frame in ("▰▱▱", "▱▰▱", "▱▱▰"))
                        for v, text in toolbars
                    ):
                        continue
                    assert view.busy and not view.queue
                    os.write(master, b"\x1b\x1b")
                    stage = 4
                elif stage == 4 and not view.busy and not view.running:
                    completed.set()
                    os.write(master, b"exit\r")
                    stage = 5
            except BaseException as error:
                errors.append(error)
                os.write(master, b"exit\r")
                return

    try:
        with (
            os.fdopen(os.dup(slave), "r", encoding="utf-8") as stdin,
            os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1) as stdout,
            closing(create_input(stdin=stdin)) as input_device,
        ):
            monkeypatch.setattr("sys.stdout", stdout)
            output = Vt100_Output(
                stdout, lambda: Size(rows=28, columns=120), enable_cpr=True
            )
            worker = threading.Thread(target=drive, daemon=True)
            worker.start()
            try:
                assert (
                    run_prompt_toolkit_cli(
                        CLIArgs(root=str(tmp_path)),
                        backend,
                        [],
                        active_session=backend.session,
                        pt_input=input_device,
                        pt_output=output,
                        on_app_created=on_created,
                    )
                    == 0
                )
            finally:
                stopped.set()
                provider.release_first.set()
                worker.join(timeout=2)
        assert not errors
        assert completed.is_set()
        assert any("queued" in text and "second" in text for _, text in toolbars)
        assert all(
            "queued" not in text
            for view, text in toolbars
            if view.running and view.running.prompt == "first" and not view.queue
        )
        inputs = backend.connection.session_request(
            "GET", "/input", params={"live": "false"}
        )["data"]["inputs"]
        assert [(item["prompt"], item["outcome"]) for item in inputs] == [
            ("first", "completed"),
            ("second", "stopped"),
        ]
    finally:
        os.close(master)
        os.close(slave)
