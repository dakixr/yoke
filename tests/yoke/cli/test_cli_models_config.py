"""Regression tests for model configuration persistence."""

# ruff: noqa: D103, S101

from __future__ import annotations

from pathlib import Path

from yoke.cli.config import CLIArgs
from yoke.cli.config.providers import prepare_provider_args
from yoke.cli.models_app import set_default_model
from yoke.cli.providers.state import apply_session_provider_defaults
from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.tools.policy import PiConfig
from yoke.cli.tools.policy import ToolPolicy


def test_set_default_model_preserves_tool_capability_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
{
  "capabilities": {"shell": "allow", "mcp": "allow"},
  "tools": {"read_outlook_emails": "allow"},
  "default_model": "demo:gpt-old",
  "default_reasoning_effort": "high"
}
""".strip(),
        encoding="utf-8",
    )

    set_default_model(
        "demo:gpt-new",
        root=tmp_path,
        repo_scope=True,
    )

    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.capabilities == {
        "shell": ToolPolicy.allow,
        "mcp": ToolPolicy.allow,
    }
    assert updated.tools == {"read_outlook_emails": ToolPolicy.allow}
    assert updated.default_model == "demo:gpt-new"
    assert updated.default_reasoning_effort is None


def test_set_default_zai_model_persists_model_default_effort(tmp_path: Path) -> None:
    set_default_model(
        "zai:glm-5.2",
        root=tmp_path,
        repo_scope=True,
    )

    config_path = tmp_path / ".yoke" / "config.json"
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "zai:glm-5.2"
    assert updated.default_reasoning_effort == "thinking"


def test_config_accepts_provider_specific_thinking_effort() -> None:
    config = PiConfig(default_reasoning_effort="thinking")

    assert config.default_reasoning_effort == "thinking"
    assert (
        PiConfig.model_validate_json(config.model_dump_json()).default_reasoning_effort
        == "thinking"
    )


def test_explicit_model_does_not_inherit_incompatible_config_effort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"default_reasoning_effort": "medium"}',
        encoding="utf-8",
    )
    args = CLIArgs(model="zai:glm-5.2", root=str(tmp_path))

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.2"
    assert args.reasoning_effort == "thinking"


def test_configured_model_replaces_stale_effort_with_model_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
{
  "default_model": "zai:glm-5.2",
  "default_reasoning_effort": "medium"
}
""".strip(),
        encoding="utf-8",
    )
    args = CLIArgs(root=str(tmp_path))

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.2"
    assert args.reasoning_effort == "thinking"


def test_resumed_model_replaces_stale_effort_with_model_default(
    tmp_path: Path,
) -> None:
    args = CLIArgs(root=str(tmp_path))
    apply_session_provider_defaults(
        args,
        ProviderSessionState(
            provider_name="zai",
            model_id="glm-5.2",
            reasoning_effort="medium",
        ),
    )

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.2"
    assert args.reasoning_effort == "thinking"
