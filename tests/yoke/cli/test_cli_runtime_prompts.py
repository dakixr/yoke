from __future__ import annotations

# ruff: noqa: F403, F405
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart

from .support import *  # noqa: F403, F405


def test_cli_seeds_interactive_prompt(monkeypatch) -> None:
    seen: dict[str, list[Message]] = {}

    def fake_run_interactive_cli(
        args: CLIArgs,
        agent: FakeAgent,
        session_messages: list[Message],
        *,
        active_session,
        input_func,
        stdout,
        stderr,
        replay_session: bool = False,
    ) -> int:
        del (
            args,
            agent,
            active_session,
            input_func,
            stdout,
            stderr,
            replay_session,
        )
        seen["session_messages"] = list(session_messages)
        return 0

    monkeypatch.setattr(
        "yoke.cli.interactive.run_interactive_cli",
        fake_run_interactive_cli,
    )
    exit_code = run_cli(CLIArgs(prompt="hello world"), agent=FakeAgent())

    assert exit_code == 0
    assert seen["session_messages"][-1].role == "user"
    assert seen["session_messages"][-1].content == "hello world"


def test_main_prints_concise_usage_errors(capsys) -> None:
    exit_code = main(["tools", "bogus"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "No such command 'bogus'" in captured.err
    assert "Traceback" not in captured.err


def test_cli_runs_headless_prompt(capsys) -> None:
    exit_code = run_cli(CLIArgs(prompt="hello world", headless=True), agent=FakeAgent())

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "synthetic response"


def test_cli_reads_headless_prompt_from_stdin(monkeypatch) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    exit_code = run_cli(
        CLIArgs(headless=True),
        agent=FakeAgent(),
        input_func=lambda _="": "prompt from stdin\n",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue().strip() == "synthetic response"
    assert stderr.getvalue() == ""


def test_cli_headless_accepts_image_attachments(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
    )
    agent = ImageAwareAgent()

    exit_code = run_cli(
        CLIArgs(
            prompt="describe [tiny.png] please",
            headless=True,
            images=(str(image_path),),
            session="image-demo",
            root=str(tmp_path),
        ),
        agent=agent,
    )

    assert exit_code == 0
    assert len(agent.seen_user_messages) == 1
    assert agent.seen_user_messages[0].text_content() == "describe [tiny.png] please"
    content = agent.seen_user_messages[0].content
    assert isinstance(content, list)
    text_part = content[0]
    image_part = content[1]
    assert isinstance(text_part, MessageTextContentPart)
    assert text_part.text == "describe [tiny.png] please"
    assert isinstance(image_part, MessageLocalImageContentPart)
    assert Path(image_part.path) == image_path.resolve()
    assert image_part.label == "[Image #1]"

    stored = SessionStore(session_dir).load("image-demo")
    stored_messages = stored.messages
    assert stored_messages[0].text_content() == "describe [tiny.png] please"
    stored_content = stored_messages[0].content
    assert isinstance(stored_content, list)
    stored_text_part = stored_content[0]
    stored_image_part = stored_content[1]
    assert isinstance(stored_text_part, MessageTextContentPart)
    assert stored_text_part.text == "describe [tiny.png] please"
    assert isinstance(stored_image_part, MessageLocalImageContentPart)
    assert stored_image_part.label == "[Image #1]"


def test_cli_requires_prompt_in_headless_mode(monkeypatch) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    exit_code = run_cli(
        CLIArgs(headless=True),
        agent=FakeAgent(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert (
        "Headless mode requires --prompt or prompt text from stdin."
        in stderr.getvalue()
    )
