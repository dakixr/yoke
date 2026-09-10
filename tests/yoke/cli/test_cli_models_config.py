"""Regression tests for model configuration persistence."""

# ruff: noqa: D103, S101

from __future__ import annotations

from pathlib import Path

import pytest

from yoke.ai.providers.model_selection import UnknownModelError
from yoke.cli.config import CLIArgs
from yoke.cli.config import build_cli_agent_from_args
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
        "zai:glm-5.3-flash",
        root=tmp_path,
        repo_scope=True,
    )

    config_path = tmp_path / ".yoke" / "config.json"
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "zai:glm-5.3-flash"
    assert updated.default_reasoning_effort == "max"


def test_set_default_model_accepts_sdk_style_thinking_effort(tmp_path: Path) -> None:
    set_default_model(
        "zai:glm-5.3-flash:low",
        root=tmp_path,
        repo_scope=True,
    )

    config_path = tmp_path / ".yoke" / "config.json"
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "zai:glm-5.3-flash"
    assert updated.default_reasoning_effort == "low"


def test_set_default_model_rejects_unknown_catalog_model(tmp_path: Path) -> None:
    with pytest.raises(UnknownModelError, match="Unknown model 'glm-missing'"):
        set_default_model(
            "zai:glm-missing",
            root=tmp_path,
            repo_scope=True,
        )

    assert not (tmp_path / ".yoke" / "config.json").exists()


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
    args = CLIArgs(model="zai:glm-5.3-flash", root=str(tmp_path))

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.3-flash"
    assert args.reasoning_effort == "max"
    assert args.model_source == "cli"


def test_explicit_model_accepts_sdk_style_thinking_effort(tmp_path: Path) -> None:
    args = CLIArgs(model="zai:glm-5.3-flash:low", root=str(tmp_path))

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.3-flash"
    assert args.reasoning_effort == "low"
    assert args.model_source == "cli"


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
  "default_model": "zai:glm-5.3-flash",
  "default_reasoning_effort": "medium"
}
""".strip(),
        encoding="utf-8",
    )
    args = CLIArgs(root=str(tmp_path))

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.3-flash"
    assert args.reasoning_effort == "max"
    assert args.model_source == "config"


def test_prepare_provider_args_loads_effective_config_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_reads: list[tuple[Path, Path | None]] = []

    def load_config(*, root: Path, home: Path | None = None) -> PiConfig:
        config_reads.append((root, home))
        return PiConfig(
            default_model="zai:glm-5.3-flash",
            default_reasoning_effort="medium",
        )

    monkeypatch.setattr(
        "yoke.cli.config.providers.load_effective_yoke_config",
        load_config,
    )
    args = CLIArgs(root=str(tmp_path))

    prepare_provider_args(args)

    assert config_reads == [(tmp_path, tmp_path)]
    assert args.provider_name == "zai"
    assert args.model == "glm-5.3-flash"
    assert args.reasoning_effort == "max"
    assert args.model_source == "config"


def test_resumed_model_replaces_stale_effort_with_model_default(
    tmp_path: Path,
) -> None:
    args = CLIArgs(root=str(tmp_path))
    apply_session_provider_defaults(
        args,
        ProviderSessionState(
            provider_name="zai",
            model_id="glm-5.3-flash",
            reasoning_effort="medium",
        ),
    )

    prepare_provider_args(args)

    assert args.provider_name == "zai"
    assert args.model == "glm-5.3-flash"
    assert args.reasoning_effort == "max"
    assert args.model_source == "session"


def test_interactive_cli_recovers_stale_configured_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"default_model": "zai:glm-missing", "default_reasoning_effort": "low"}',
        encoding="utf-8",
    )
    args = CLIArgs(root=str(tmp_path))

    built = build_cli_agent_from_args(args, recover_model=True)
    try:
        assert args.provider_name == "zai"
        assert args.model == "glm-5.3-flash"
        assert args.reasoning_effort == "max"
        assert args.model_source == "config"
        assert built.startup_warning is not None
        assert "Configured model zai:glm-missing is no longer available" in (
            built.startup_warning
        )
        assert "Started with zai:glm-5.3-flash" in built.startup_warning
        assert "configured default was not changed" in built.startup_warning
        assert '"default_model": "zai:glm-missing"' in config_path.read_text()
    finally:
        built.agent.close()


def test_headless_cli_rejects_stale_configured_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"default_model": "zai:glm-missing"}',
        encoding="utf-8",
    )

    with pytest.raises(UnknownModelError, match="Unknown model 'glm-missing'"):
        build_cli_agent_from_args(
            CLIArgs(root=str(tmp_path), headless=True),
            recover_model=True,
        )


def test_explicit_cli_model_remains_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    args = CLIArgs(model="zai:glm-missing", root=str(tmp_path))

    with pytest.raises(UnknownModelError, match="Unknown model 'glm-missing'"):
        build_cli_agent_from_args(args, recover_model=True)

    assert args.model_source == "cli"


def test_resume_recovers_stale_saved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    args = CLIArgs(root=str(tmp_path))
    apply_session_provider_defaults(
        args,
        ProviderSessionState(
            provider_name="zai",
            model_id="glm-missing",
            reasoning_effort="low",
        ),
    )

    built = build_cli_agent_from_args(args, recover_model=True)
    try:
        assert args.provider_name == "zai"
        assert args.model == "glm-5.3-flash"
        assert args.reasoning_effort == "max"
        assert args.model_source == "session"
        assert built.startup_warning is not None
        assert "Saved model zai:glm-missing is no longer available" in (
            built.startup_warning
        )
        assert "Resumed with zai:glm-5.3-flash" in built.startup_warning
    finally:
        built.agent.close()
