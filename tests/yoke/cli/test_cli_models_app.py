from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path
from typing import Any
from typing import Callable
from typing import cast

import httpx
import pytest
from typer.testing import CliRunner
from typer.testing import Result

from yoke.agent.models import Message
from yoke.cli.main import app
from yoke.cli.tools.policy import PiConfig
from yoke.ai.providers.opencode_go import (
    OpenCodeGoConfig,
    OpenCodeGoProvider,
    list_provider_models as list_opencode_go_models,
)


def _invoke_models_set_with_home(
    tmp_path: Path,
    home: Path,
    *args: str,
) -> Result:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("yoke.cli.models_app.Path.home", lambda: home)
    try:
        return CliRunner().invoke(app, ["models", "set", *args])
    finally:
        monkeypatch.undo()


def test_models_list_shows_provider_qualified_models_and_default(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini"}\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["models", "list", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "Model Inventory" in result.stdout
    assert "codex:gpt-5.4-mini" in result.stdout
    assert "opencode-go" in result.stdout
    assert "gpt-5.5" in result.stdout
    assert "Configured default model: codex:gpt-5.4-mini" in result.stdout


def test_opencode_go_catalog_includes_kimi_k2_7_code() -> None:
    models = {model.id: model for model in list_opencode_go_models(None)}

    kimi = models["kimi-k2.7-code"]
    assert kimi.display_name == "Kimi K2.7 Code"
    assert kimi.supports_image_inputs is True
    assert kimi.context_window_tokens == 262_144
    assert kimi.thinking_levels == ()
    assert kimi.default_thinking_level is None


def test_opencode_go_catalog_matches_current_reasoning_efforts() -> None:
    expected = {
        "glm-5.2": (),
        "glm-5.1": (),
        "glm-5": (),
        "kimi-k2.7-code": (),
        "kimi-k2.6": (),
        "kimi-k2.5": (),
        "deepseek-v4-pro": ("high", "max"),
        "deepseek-v4-flash": ("high", "max"),
        "mimo-v2.5": (),
        "mimo-v2-omni": (),
        "mimo-v2-pro": (),
        "mimo-v2.5-pro": (),
        "minimax-m3": ("none", "thinking"),
        "minimax-m2.7": (),
        "minimax-m2.5": (),
        "qwen3.7-plus": (),
        "qwen3.6-plus": (),
        "qwen3.5-plus": (),
    }
    models = {model.id: model for model in list_opencode_go_models(None)}

    assert {
        model_id: models[model_id].thinking_levels for model_id in expected
    } == expected


def test_opencode_go_config_accepts_persisted_reasoning_efforts() -> None:
    assert (
        OpenCodeGoConfig(
            api_key="test",
            model="kimi-k2.7-code",
            reasoning_effort="Low",
        ).reasoning_effort
        == "low"
    )


def test_opencode_go_config_rejects_unknown_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        OpenCodeGoConfig(
            api_key="test",
            model="kimi-k2.7-code",
            reasoning_effort="turbo",
        )


def test_opencode_go_openai_models_send_high_max_tokens() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="kimi-k2.7-code"),
        http_client=client,
    )

    try:
        provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    assert '"max_tokens":65536' in cast(str, captured["payload"])


def test_opencode_go_accepts_only_catalog_reasoning_efforts() -> None:
    invalid_efforts = (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "thinking",
    )
    provider = OpenCodeGoProvider(OpenCodeGoConfig(api_key="test"))

    try:
        for model in list_opencode_go_models(None):
            for effort in model.thinking_levels:
                provider.set_model(model.id, reasoning_effort=effort)
                assert provider.config.model == model.id
                assert provider.config.reasoning_effort == effort
            for effort in set(invalid_efforts) - set(model.thinking_levels):
                with pytest.raises(ValueError, match="Unsupported reasoning effort"):
                    provider.set_model(model.id, reasoning_effort=effort)
    finally:
        provider.close()


def test_models_set_writes_repo_default_model_and_preserves_tools(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_path = home / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"tools": {"read": "allow"}}\n',
        encoding="utf-8",
    )

    result = _invoke_models_set_with_home(
        tmp_path,
        home,
        "codex:gpt-5.4-mini",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    assert (
        "Set default_model=codex:gpt-5.4-mini in ~\\.yoke\\config.json" in result.stdout
    )
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "codex:gpt-5.4-mini"
    assert updated.tools["read"].value == "allow"


def test_models_set_persists_default_reasoning_effort(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_path = home / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)

    result = _invoke_models_set_with_home(
        tmp_path,
        home,
        "codex:gpt-5.4-mini",
        "--reasoning-effort",
        "high",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    assert (
        "Set default_model=codex:gpt-5.4-mini default_reasoning_effort=high"
    ) in result.stdout
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "codex:gpt-5.4-mini"
    assert updated.default_reasoning_effort == "high"


def test_models_set_supports_global_config_scope(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.models_app.Path.home", lambda: home)

    result = CliRunner().invoke(
        app,
        [
            "models",
            "set",
            "zai:glm-5.1",
            "--global",
        ],
    )

    config_path = home / ".yoke" / "config.json"
    assert result.exit_code == 0
    assert "Set default_model=zai:glm-5.1 in ~\\.yoke\\config.json" in result.stdout
    updated = PiConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert updated.default_model == "zai:glm-5.1"


def test_models_set_interactive_selector_shows_model_metadata_columns(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.cli import models_app

    captured: dict[str, object] = {}

    def fake_select_table_item_interactive(items, **kwargs):
        captured["items"] = items
        captured.update(kwargs)
        return items[0]

    monkeypatch.setattr(models_app.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        models_app,
        "select_table_item_interactive",
        fake_select_table_item_interactive,
    )

    selected = models_app._prompt_for_default_model(root=tmp_path)

    assert selected.startswith("codex:")
    columns = cast(Any, captured["columns"])
    assert columns.headers == (
        "#",
        "Provider",
        "Model",
        "Images",
        "Context",
        "Thinking",
    )
    render_row = cast(
        Callable[[object, int, bool, object], str], captured["render_row"]
    )
    items = cast(list[object], captured["items"])
    rendered = render_row(items[0], 0, True, columns)
    assert "codex" in rendered
    assert "yes" in rendered


def test_models_set_rejects_invalid_identifier(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["models", "set", "codex", "--root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "provider-name:model-name" in result.stderr


def test_models_set_reports_invalid_existing_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_path = home / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"tools": { / "read": "allow"}}\n',
        encoding="utf-8",
    )

    result = _invoke_models_set_with_home(
        tmp_path,
        home,
        "codex:gpt-5.4-mini",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 1
    assert "Could not update default model because" in result.stderr
    assert "Fix or remove that file first." in result.stderr


def test_models_list_shows_default_reasoning_effort(tmp_path: Path) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"default_model": "codex:gpt-5.4-mini", '
        '"default_reasoning_effort": "medium"}\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["models", "list", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "Configured default reasoning effort: medium" in result.stdout
