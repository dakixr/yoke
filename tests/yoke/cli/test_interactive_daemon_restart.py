"""A live terminal survives SIGKILL of its isolated fixture daemon."""

from __future__ import annotations

import os
import signal

from tests.yoke.daemon.support import eventually, sandbox as sandbox
from yoke.cli.config import CLIArgs

from .support import CaptureStream


def test_prompt_ui_reattaches_after_daemon_restart_before_next_prompt(
    sandbox, monkeypatch
):
    from threading import Thread

    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from yoke.cli.backend.connection import SessionConnection
    from yoke.cli.backend.daemon import DaemonSessionBackend
    from yoke.cli.backend.startup import create_session
    from yoke.cli.interactive.prompt.app import run_prompt_toolkit_cli

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "10")
    output = CaptureStream()
    monkeypatch.setattr("sys.stdout", output)
    args = CLIArgs(root=str(sandbox.root))
    connection = SessionConnection(
        "prompt-restart", cwd=sandbox.root, discover=sandbox.connect
    )
    errors: list[BaseException] = []
    pids = []
    try:
        with connection.startup_lease():
            data = create_session(connection, args, root=sandbox.root)["data"]
            connection.start()
        backend = DaemonSessionBackend(connection, data)
        with create_pipe_input() as pipe:

            def drive():
                try:
                    pipe.send_text("before crash\r")
                    eventually(lambda: "echo:before crash" in output.getvalue())
                    pids.append(sandbox.status()["pid"])
                    os.kill(pids[0], signal.SIGKILL)
                    pipe.send_text("after crash\r")
                    eventually(
                        lambda: "echo:after crash" in output.getvalue(), timeout=12
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    pipe.send_text("exit\r")

            worker = Thread(target=drive, daemon=True)
            worker.start()
            result = run_prompt_toolkit_cli(
                args,
                backend,
                [],
                active_session=backend.session,
                pt_input=pipe,
                pt_output=DummyOutput(),
            )
            worker.join(timeout=15)
            assert not worker.is_alive()
        assert not errors, (errors, output.getvalue())
        assert result == 0
        assert pids[0] != sandbox.status()["pid"]
        assert output.getvalue().count("user before crash") == 1
        assert output.getvalue().count("user after crash") == 1
        assert output.getvalue().count("echo:before crash") == 1
        assert output.getvalue().count("echo:after crash") == 1
    finally:
        connection.close()
