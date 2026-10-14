"""Thinking selections must survive CLI parsing and never silently change."""

# ruff: noqa: D103, S101

from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.ai.providers.model_selection import default_reasoning_effort_for_model
from yoke.ai.providers.resolution import BUILTIN_PROVIDER_NAMES, list_provider_models
from yoke.cli.config import CLIArgs
from yoke.cli.config.providers import build_provider_from_args, prepare_provider_args
from yoke.cli.main import app
from yoke.cli.tools.policy import PiConfig


@pytest.mark.parametrize("provider", BUILTIN_PROVIDER_NAMES)
@pytest.mark.parametrize("scope", ["default", "global", "repo"])
def test_models_set_round_trips_every_advertised_thinking_level(
    tmp_path: Path, provider: str, scope: str
) -> None:
    runner = CliRunner()
    models = list_provider_models(provider, home=Path.home())
    assert models
    path = (tmp_path if scope == "repo" else Path.home()) / ".yoke/config.json"
    for model in models:
        for effort in (None, *model.thinking_levels):
            selection = f"{provider}:{model.id}" + (f":{effort}" if effort else "")
            argv = ["models", "set", selection, "--root", str(tmp_path)]
            if scope != "default":
                argv.append(f"--{scope}")

            result = runner.invoke(app, argv)

            assert result.exit_code == 0, result.output
            expected = effort or default_reasoning_effort_for_model(model)
            config = PiConfig.model_validate_json(path.read_text())
            assert config.default_model == f"{provider}:{model.id}"
            assert config.default_reasoning_effort == expected
            args = CLIArgs(root=str(tmp_path))
            prepare_provider_args(args)
            assert (args.provider_name, args.model, args.reasoning_effort) == (
                provider,
                model.id,
                expected,
            )


@pytest.mark.parametrize("scope", ["default", "global", "repo"])
@pytest.mark.parametrize(
    ("selection", "error"),
    [
        ("codex:gpt-6.1-sol:ultra", "Supported: low, medium, high, xhigh, max"),
        ("zai:glm-5.3-flash:medium", "Supported: low, high, max"),
        ("opencode-go:glm-5.3-flash:high", "no configurable levels"),
        ("codex:gpt-6.1-sol:", "non-empty thinking effort"),
    ],
)
def test_models_set_invalid_effort_preserves_config(
    tmp_path: Path, scope: str, selection: str, error: str
) -> None:
    path = (tmp_path if scope == "repo" else Path.home()) / ".yoke/config.json"
    path.parent.mkdir(parents=True)
    original = b'{"default_model":"zai:glm-5.3-flash","tools":{"read":"deny"}}\n'
    path.write_bytes(original)
    argv = ["models", "set", selection, "--root", str(tmp_path)]
    if scope != "default":
        argv.append(f"--{scope}")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code != 0
    assert error in result.output
    assert path.read_bytes() == original


def test_invalid_effort_without_explicit_model_rejects_provider_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    args = CLIArgs(provider_name="zai", reasoning_effort="medium", root=str(tmp_path))
    prepare_provider_args(args)

    with pytest.raises(ValueError, match="Unsupported thinking effort 'medium'"):
        build_provider_from_args(args)


def test_models_set_normalizes_effort_case(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "models",
            "set",
            "codex:gpt-6.1-sol: HIGH ",
            "--repo",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    config = PiConfig.model_validate_json((tmp_path / ".yoke/config.json").read_text())
    assert config.default_reasoning_effort == "high"


@pytest.mark.parametrize("provider", BUILTIN_PROVIDER_NAMES)
def test_every_advertised_level_reaches_provider_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    for name in ("YOKE_CODEX_API_KEY", "OPENCODE_API_KEY", "ZAI_API_KEY"):
        monkeypatch.setenv(name, "test-key")
    models = list_provider_models(provider, home=Path.home())
    assert models
    for model in models:
        for effort in (None, *model.thinking_levels):
            selection = f"{provider}:{model.id}" + (f":{effort}" if effort else "")
            args = CLIArgs(model=selection, root=str(tmp_path))
            prepare_provider_args(args)
            instance = build_provider_from_args(args)
            try:
                config = getattr(instance, "config", None)
                assert getattr(config, "model", None) == model.id
                assert getattr(config, "reasoning_effort", None) == (
                    effort or default_reasoning_effort_for_model(model)
                )
            finally:
                close = getattr(instance, "close", None)
                if callable(close):
                    close()
