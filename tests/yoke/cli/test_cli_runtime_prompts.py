from __future__ import annotations

from pathlib import Path
from threading import Event


from yoke.cli.config import CLIArgs
from yoke.cli.interactive.common import (
    TurnSuccess,
)
from yoke.cli.backend.startup import run_daemon_cli
from yoke.agent.loop import RuntimeAgent
from tests.yoke.daemon.support import eventually
from .terminal_daemon_support import (
    ReplyProvider,
    terminal as terminal,
)
from yoke.session import SessionStore

from .support import CaptureStream


def test_headless_cli_generates_title_without_blocking_user_turn(
    tmp_path, monkeypatch, terminal
) -> None:
    title_started, response_written, user_turn_started = Event(), Event(), Event()
    title_messages = []

    def generate(_factory, _record, messages):
        title_messages.extend(message.plain_text_content or "" for message in messages)
        title_started.set()
        assert response_written.wait(timeout=5)
        return "Generated Background Title"

    class ConcurrentProvider(ReplyProvider):
        def complete(self, messages, tools):
            assert title_started.wait(timeout=5)
            user_turn_started.set()
            return super().complete(messages, tools)

    class SignalingOutput(CaptureStream):
        def write(self, text: str) -> int:
            written = super().write(text)
            if "synthetic response" in self.getvalue():
                response_written.set()
            return written

    terminal.factory = lambda record: RuntimeAgent(
        ConcurrentProvider(), [], tool_root=Path(record.root)
    )
    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)
    output = SignalingOutput()
    exit_code = run_daemon_cli(
        CLIArgs(prompt="Reply with exactly OK.", headless=True, root=str(tmp_path)),
        stdout=output,
        stderr=CaptureStream(),
    )
    assert exit_code == 0
    assert user_turn_started.is_set()
    assert title_messages == ["Reply with exactly OK."]
    eventually(
        lambda: (
            SessionStore().list(root=tmp_path)[0].title == "Generated Background Title"
        )
    )


def test_session_title_worker_is_deduplicated(tmp_path, monkeypatch, terminal) -> None:
    started, release = Event(), Event()
    calls = []

    def generate(*_args, **_kwargs):
        calls.append("title")
        started.set()
        assert release.wait(timeout=5)
        return "One Generated Title"

    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)
    backend = terminal.backend("deduplicated-title", root=tmp_path)
    first = backend.submit("first prompt", user_message=None)
    try:
        assert started.wait(timeout=5)
        list(first)
        assert isinstance(first.result(), TurnSuccess)
        second = backend.submit("second prompt", user_message=None)
        list(second)
        assert isinstance(second.result(), TurnSuccess)
        assert calls == ["title"]
    finally:
        release.set()
    eventually(
        lambda: SessionStore().load("deduplicated-title").title == "One Generated Title"
    )


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
