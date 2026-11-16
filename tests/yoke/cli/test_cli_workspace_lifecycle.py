"""Live deletion, fresh configuration and resource cleanup at CLI boundaries."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations


from pathlib import Path


from yoke.agent.models import Message, MessageTextContentPart
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.config import CLIArgs
from yoke.cli.config import runtime as config_runtime
from yoke.cli.backend.startup import run_daemon_interactive
from yoke.session import SessionStore

from .support import CaptureStream
from .terminal_daemon_support import terminal as terminal


def test_relocation_rediscovers_configuration_and_skills(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    target = tmp_path / "target"
    skill_dir = target / ".yoke" / "skills" / "fresh-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fresh-skill\ndescription: Newly discovered skill.\n---\nNew instructions.\n",
        encoding="utf-8",
    )
    (target / "AGENTS.md").write_text("TARGET WORKSPACE INSTRUCTIONS", encoding="utf-8")
    (target / ".yoke" / "config.json").write_text(
        '{"tools": {"read": "deny"}}', encoding="utf-8"
    )
    store = SessionStore()
    historical_skill = ActiveSkill(
        name="old-skill",
        description="Historical skill",
        source_path=str(tmp_path / "missing" / "SKILL.md"),
        content="Cached old instructions",
    )
    original = store.save(
        "saved",
        [Message.user("old history")],
        root=tmp_path / "missing",
        active_skills=[historical_skill],
        skill_dirs=[str(tmp_path / "old-skills")],
    )
    monkeypatch.setattr(
        config_runtime,
        "_build_startup_provider",
        lambda args, **kwargs: (ToolDiscoveryProvider(), None),
    )
    monkeypatch.setattr(
        "yoke.http.services.session_startup.build_cli_agent_from_args",
        config_runtime.build_cli_agent_from_args,
    )
    original_resolve = config_runtime._resolve_cli_agent_config
    resolutions = []

    def resolve(**kwargs):
        resolved = original_resolve(**kwargs)
        resolutions.append(resolved)
        assert kwargs["root"] == target
        return resolved

    monkeypatch.setattr(config_runtime, "_resolve_cli_agent_config", resolve)

    def interactive(_args, agent, messages, **kwargs):
        assert any(skill.name == "fresh-skill" for skill in agent.available_skills())
        assert store.load("saved").active_skills == [historical_skill]
        assert (
            resolutions[0].tool_report.config_path == target / ".yoke" / "config.json"
        )
        assert "read" not in agent.tools().active_names()
        assert (
            store.load(kwargs["active_session"].id).conversation_entries
            == original.conversation_entries
        )
        assert messages == [
            message.model_copy(
                update={
                    "content": [
                        MessageTextContentPart(text=message.text_content() or "")
                    ]
                }
            )
            for message in original.messages
        ]
        assert any(
            "TARGET WORKSPACE INSTRUCTIONS" in message.text_content()
            for message in resolutions[0].system_messages
        )
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_daemon_interactive(
            CLIArgs(root=str(tmp_path)),
            resume_id="saved",
            relocate=target,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 0
    )
    assert store.load("saved").active_skills == [historical_skill]
