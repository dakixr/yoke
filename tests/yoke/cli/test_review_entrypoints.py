"""Piped prompts and cross-workspace resume use the public daemon path."""

from io import StringIO

import pytest
from typer.testing import CliRunner

from yoke.cli.backend.startup import run_daemon_headless, run_daemon_interactive
from yoke.cli.config import CLIArgs
from yoke.cli.main import app

from .support import CaptureStream
from .terminal_daemon_support import terminal as terminal


def test_headless_preserves_all_piped_lines(monkeypatch, terminal, tmp_path):
    text = "first line\n\nthird line\nlast line\n"
    monkeypatch.setattr("sys.stdin", StringIO(text))
    output, error = CaptureStream(), CaptureStream()
    assert (
        run_daemon_headless(
            CLIArgs(headless=True, session="piped", root=str(tmp_path)),
            stdout=output,
            stderr=error,
        )
        == 0
    )
    assert terminal.store.load("piped").messages[0].text_content() == text.strip()
    assert error.getvalue() == ""


@pytest.mark.parametrize("all_sessions", [False, True])
def test_resume_picker_lists_requested_roots(terminal, tmp_path, all_sessions):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    terminal.store.save("local", [], root=here, title="local title")
    terminal.store.save("elsewhere", [], root=there, title="other title")
    output, error = CaptureStream(), CaptureStream()
    assert (
        run_daemon_interactive(
            CLIArgs(root=str(here)),
            picker=True,
            all_sessions=all_sessions,
            input_func=lambda prompt: "invalid",
            stdout=output,
            stderr=error,
        )
        == 1
    )
    assert "local title" in output.getvalue()
    assert ("other title" in output.getvalue()) is all_sessions
    assert "Invalid session selection" in error.getvalue()


def test_resume_all_is_forwarded_to_picker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "yoke.cli.backend.startup.run_daemon_interactive",
        lambda *args, **kwargs: calls.append(kwargs) or 0,
    )
    result = CliRunner().invoke(app, ["resume", "--all"])
    assert result.exit_code == 0
    assert calls == [{"picker": True, "all_sessions": True}]


def test_resume_all_keeps_chosen_workspace(terminal, tmp_path, monkeypatch):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    terminal.store.save("elsewhere", [], root=there, title="other title")
    seen = []

    def interactive(args, backend, messages, **kwargs):
        seen.append((args.root, backend.session.id, kwargs["replay_session"]))
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_daemon_interactive(
            CLIArgs(root=str(here)),
            picker=True,
            all_sessions=True,
            input_func=lambda prompt: "1",
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 0
    )
    assert seen == [(str(there), "elsewhere", True)]
    assert terminal.store.load("elsewhere").root == str(there)
