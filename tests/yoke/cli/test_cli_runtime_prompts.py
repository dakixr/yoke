from __future__ import annotations


from yoke.cli.config import CLIArgs
from yoke.cli.backend.startup import run_daemon_cli
from .terminal_daemon_support import (
    terminal as terminal,
)

from .support import CaptureStream


def test_cli_reads_headless_prompt_from_stdin(monkeypatch, terminal) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    exit_code = run_daemon_cli(
        CLIArgs(headless=True),
        input_func=lambda _="": "prompt from stdin\n",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue().strip() == "synthetic response"
    assert stderr.getvalue() == ""


def test_cli_requires_prompt_in_headless_mode(monkeypatch, terminal) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    exit_code = run_daemon_cli(
        CLIArgs(headless=True),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert (
        "Headless mode requires --prompt or prompt text from stdin."
        in stderr.getvalue()
    )
