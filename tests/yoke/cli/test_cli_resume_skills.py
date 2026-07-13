from __future__ import annotations

# ruff: noqa: ANN001, D100, D103, S101

from pathlib import Path
from typing import cast

import pytest

from yoke.agent.models import Message
from yoke.agent.skills.registry import load_skill_registry
from yoke.cli.config import CLIArgs
from yoke.cli.main import run_resume_cli
from yoke.cli.session import SessionStore

from .support import CaptureStream
from .support import CatalogProvider
from .support import install_builtin_provider


@pytest.mark.parametrize("mutation", ["missing_file", "renamed_directory"])
def test_resume_survives_invalid_skill_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.bootstrap.config.Path.home", lambda: home)
    seen_prompts: list[str] = []

    class SkillPromptProvider(CatalogProvider):
        def complete(self, messages: object, tools: object) -> Message:
            resolved_messages = cast(list[Message], messages)
            seen_prompts.extend(
                message.text_content() or "" for message in resolved_messages
            )
            return super().complete(messages, tools)

    install_builtin_provider(monkeypatch, SkillPromptProvider)
    skill_dir = tmp_path / ".yoke" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n\nBe durable.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([skill_dir.parent])
    store = SessionStore()
    active_skill = registry.activate("demo-skill")
    active_skill.reload_on_next_use = False
    store.save(
        "resume-skills",
        [Message.user("old"), Message.assistant("answer")],
        active_skills=[active_skill],
        root=tmp_path,
        title="Skill resume",
        provider_name="codex",
        model_id="gpt-5.4",
    )
    if mutation == "missing_file":
        skill_file.unlink()
    else:
        skill_dir.rename(skill_dir.with_name("renamed-skill"))

    stdout = CaptureStream()
    stderr = CaptureStream()
    prompts = iter(["next"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "resume-skills",
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "1 skill load failure(s)" in stdout.getvalue()
    assert "ok" in stdout.getvalue()
    assert any("Be durable." in prompt for prompt in seen_prompts)
    assert "Invalid skill directory" not in stderr.getvalue()
    saved = store.load("resume-skills")
    assert len(saved.active_skills) == 1
    assert saved.active_skills[0].content is not None
    assert "Be durable." in saved.active_skills[0].content


def test_resume_clears_unavailable_legacy_active_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yoke.agent.skills.models import ActiveSkill

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.bootstrap.config.Path.home", lambda: home)
    install_builtin_provider(monkeypatch, CatalogProvider)
    store = SessionStore()
    store.save(
        "legacy-skill",
        [Message.user("old"), Message.assistant("answer")],
        active_skills=[
            ActiveSkill(
                name="removed-skill",
                description="Removed skill.",
                source_path=str(tmp_path / "removed-skill" / "SKILL.md"),
            )
        ],
        root=tmp_path,
        title="Legacy skill",
        provider_name="codex",
        model_id="gpt-5.4",
    )
    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "legacy-skill",
        input_func=fake_input,
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert store.load("legacy-skill").active_skills == []
