from __future__ import annotations

# ruff: noqa: ANN001, ANN201, D100, D103, S101

from pathlib import Path
from typing import Any
from typing import cast

import pytest

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.config import build_agent_from_args
from yoke.cli.runtime.session import apply_session_defaults_to_args
from yoke.cli.session import SessionRecord
from yoke.cli.tools.policy import PiConfig

from .support import install_builtin_provider


def test_yoke_config_accepts_valid_default_model() -> None:
    config = PiConfig.model_validate({"default_model": "Codex:gpt-5.4-mini"})

    assert config.default_model == "codex:gpt-5.4-mini"


def test_yoke_config_accepts_model_id_with_colon() -> None:
    config = PiConfig.model_validate({"default_model": "Demo:provider.model-name"})

    assert config.default_model == "demo:provider.model-name"


def test_yoke_config_accepts_default_reasoning_effort() -> None:
    config = PiConfig.model_validate({"default_reasoning_effort": "High"})

    assert config.default_reasoning_effort == "high"


@pytest.mark.parametrize(
    "value",
    ["codex", "codex:", ":gpt-5.4", "   "],
)
def test_yoke_config_rejects_invalid_default_model(value: str) -> None:
    with pytest.raises(ValueError):
        PiConfig.model_validate({"default_model": value})


def test_yoke_config_rejects_invalid_default_reasoning_effort() -> None:
    with pytest.raises(ValueError):
        PiConfig.model_validate({"default_reasoning_effort": "extreme"})


def test_build_agent_uses_config_default_model_when_no_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCodexProvider:
        def __init__(self, config) -> None:
            self.config = config

        def complete(self, messages, tools) -> Message:
            del messages, tools
            return Message.assistant("ok")

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini"}\n',
        encoding="utf-8",
    )
    install_builtin_provider(monkeypatch, FakeCodexProvider)

    agent = build_agent_from_args(CLIArgs(root=str(tmp_path)))

    assert cast(Any, agent.provider).config.model == "gpt-5.4-mini"


def test_build_agent_uses_config_default_reasoning_effort_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCodexProvider:
        def __init__(self, config) -> None:
            self.config = config

        def complete(self, messages, tools) -> Message:
            del messages, tools
            return Message.assistant("ok")

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini", "default_reasoning_effort": "high"}\n',
        encoding="utf-8",
    )
    install_builtin_provider(monkeypatch, FakeCodexProvider)

    agent = build_agent_from_args(CLIArgs(root=str(tmp_path)))

    assert cast(Any, agent.provider).config.reasoning_effort == "high"


def test_build_agent_uses_model_specific_default_reasoning_effort_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCodexProvider:
        def __init__(self, config) -> None:
            self.config = config

        def complete(self, messages, tools) -> Message:
            del messages, tools
            return Message.assistant("ok")

    install_builtin_provider(monkeypatch, FakeCodexProvider)

    mini_agent = build_agent_from_args(
        CLIArgs(model="codex:gpt-5.4-mini", root=str(tmp_path))
    )
    full_agent = build_agent_from_args(
        CLIArgs(model="codex:gpt-5.4", root=str(tmp_path))
    )

    assert cast(Any, mini_agent.provider).config.reasoning_effort == "xhigh"
    assert cast(Any, full_agent.provider).config.reasoning_effort == "medium"


def test_cli_override_beats_config_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCodexProvider:
        def __init__(self, config) -> None:
            self.config = config

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini"}\n',
        encoding="utf-8",
    )
    install_builtin_provider(monkeypatch, FakeCodexProvider)

    agent = build_agent_from_args(
        CLIArgs(
            model="codex:gpt-5.4",
            root=str(tmp_path),
        )
    )

    assert cast(Any, agent.provider).config.model == "gpt-5.4"


def test_session_resume_defaults_override_config_default_model(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini"}\n',
        encoding="utf-8",
    )
    args = CLIArgs(root=str(tmp_path))
    record = SessionRecord(
        id="session-1",
        provider_name="demo",
        model_id="provider.model-name",
        reasoning_effort="high",
    )

    apply_session_defaults_to_args(args, record)

    assert args.model == "demo:provider.model-name"
    assert args.reasoning_effort == "high"
