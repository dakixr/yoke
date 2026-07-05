from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002, ANN003, ANN401, D100, D103, F403, F405, S101

from .support import *  # noqa: F403, F405


def test_interactive_cli_can_activate_skill_with_slash_command(
    tmp_path: Path,
) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.cli.interactive.basic import run_basic_interactive_cli

    skill_dir = tmp_path / ".yoke" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code.\n---\n\nBe strict.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])
    agent = RuntimeAgent(
        provider=TitleProvider("done"),
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )
    prompts = iter(["/skill code-review", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    active_session = active_session_for(tmp_path)

    exit_code = run_basic_interactive_cli(
        CLIArgs(root=str(tmp_path)),
        agent,
        [],
        active_session=active_session,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    saved = active_session.store.load(active_session.id)
    output = stdout.getvalue()
    assert exit_code == 0
    assert [skill.name for skill in agent.active_skills] == ["code-review"]
    assert [skill.name for skill in saved.active_skills] == ["code-review"]
    assert "Activated skill: code-review" in output


def test_interactive_cli_marks_active_skill_for_reload_when_reactivated(
    tmp_path: Path,
) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.cli.interactive.basic import run_basic_interactive_cli

    skill_dir = tmp_path / ".yoke" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code.\n---\n\nBe strict.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])
    active_skill = registry.activate("code-review")
    agent = RuntimeAgent(
        provider=TitleProvider("done"),
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
        active_skills=[active_skill],
    )
    active_skill.reload_on_next_use = False
    prompts = iter(["/skill code-review", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    active_session = active_session_for(tmp_path)
    active_session.record.active_skills = [active_skill]

    exit_code = run_basic_interactive_cli(
        CLIArgs(root=str(tmp_path)),
        agent,
        [],
        active_session=active_session,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    saved = active_session.store.load(active_session.id)
    output = stdout.getvalue()
    assert exit_code == 0
    assert [skill.name for skill in agent.active_skills] == ["code-review"]
    assert [skill.name for skill in saved.active_skills] == ["code-review"]
    assert agent.active_skills[0].reload_on_next_use is True
    assert saved.active_skills[0].reload_on_next_use is True
    assert "Skill already active; reloading next use: code-review" in output


def test_interactive_cli_runs_prompt_after_skill_slash_command_semicolon(
    tmp_path: Path,
) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.cli.interactive.basic import run_basic_interactive_cli

    skill_dir = tmp_path / ".yoke" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code.\n---\n\nBe strict.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])
    provider = TitleProvider("done")
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )
    prompts = iter(["/skill code-review ; please review this"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    active_session = active_session_for(tmp_path)

    exit_code = run_basic_interactive_cli(
        CLIArgs(root=str(tmp_path)),
        agent,
        [],
        active_session=active_session,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert [skill.name for skill in agent.active_skills] == ["code-review"]
    assert provider.prompts[0] == "please review this"
    assert "Activated skill: code-review" in output
    assert "please review this" in output


def test_interactive_cli_runs_prompt_after_skill_slash_command_directly(
    tmp_path: Path,
) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.cli.interactive.basic import run_basic_interactive_cli

    skill_dir = tmp_path / ".yoke" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code.\n---\n\nBe strict.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])
    provider = TitleProvider("done")
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )
    prompts = iter(["/skill code-review please review this"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    active_session = active_session_for(tmp_path)

    exit_code = run_basic_interactive_cli(
        CLIArgs(root=str(tmp_path)),
        agent,
        [],
        active_session=active_session,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert [skill.name for skill in agent.active_skills] == ["code-review"]
    assert provider.prompts[0] == "please review this"
    assert "Activated skill: code-review" in output
    assert "please review this" in output
