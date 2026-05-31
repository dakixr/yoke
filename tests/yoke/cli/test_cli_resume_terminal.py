from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002, ANN003, ANN202, D100, D103, F401, F403, F405, S101

from .support import *  # noqa: F403, F405


def test_resume_without_id_filters_sessions_by_current_root(
    tmp_path: Path,
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    store = SessionStore()
    store.save("same-root", [], root=tmp_path, title="Same root")
    store.save("other-root", [], root=other_root, title="Other root")
    prompts = iter(["1", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        None,
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Resuming session same-root" in stdout.getvalue()
    assert "Same root (same-root)" in stdout.getvalue()
    assert "other-root" not in stdout.getvalue()


def test_resume_without_id_uses_keyboard_selector_for_terminal(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class TerminalCaptureStream(CaptureStream):
        def isatty(self) -> bool:
            return True

    store = SessionStore()
    store.save("first", [], root=tmp_path, title="First session")
    store.save("second", [], root=tmp_path, title="Second session")

    selected: dict[str, object] = {}

    def fake_selector(records: list[Any], *, root: Path, all_sessions: bool) -> str:
        selected["ids"] = [record.id for record in records]
        selected["root"] = root
        selected["all_sessions"] = all_sessions
        return "second"

    monkeypatch.setattr(
        "yoke.cli.runtime.session._select_session_id_interactive",
        fake_selector,
    )

    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = TerminalCaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        None,
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert selected["ids"] == ["second", "first"]
    assert selected["root"] == tmp_path.resolve()
    assert selected["all_sessions"] is False
    output = stdout.getvalue()
    assert "Resuming session second" in output
    assert "Session number:" not in output


def test_resume_without_id_reports_cancelled_keyboard_selector(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class TerminalCaptureStream(CaptureStream):
        def isatty(self) -> bool:
            return True

    store = SessionStore()
    store.save("saved", [], root=tmp_path, title="Saved session")
    monkeypatch.setattr(
        "yoke.cli.runtime.session._select_session_id_interactive",
        lambda records, *, root, all_sessions: None,
    )

    stdout = TerminalCaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        None,
        agent=FakeAgent(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "Session selection cancelled." in stderr.getvalue()


@pytest.mark.parametrize("exit_mode", ["keyboard_interrupt", "quit"])
def test_basic_interactive_exit_prints_resume_hint(
    tmp_path: Path, exit_mode: str
) -> None:
    def fake_input(_: object = "") -> str:
        if exit_mode == "keyboard_interrupt":
            raise KeyboardInterrupt
        return "quit"

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "To resume this session run:\nyoke resume " in stdout.getvalue()


def test_prompt_toolkit_exit_prints_resume_hint(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    install_fake_prompt_toolkit(monkeypatch, ["exit"])
    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        FakeAgent(),
        [],
        active_session=active_session_for(tmp_path),
    )

    assert exit_code == 0
    assert "To resume this session run:\nyoke resume " in capsys.readouterr().out
