"""Model validation must never damage an existing default or tool policy."""

# ruff: noqa: D103, S101

from pathlib import Path

import pytest

from yoke.ai.providers.resolution import UnknownModelError
from yoke.cli.models_app import set_default_model
from yoke.cli.tools.policy import PiConfig


@pytest.mark.parametrize("scope", ["global", "repo"])
def test_unknown_catalog_model_does_not_write_config(
    tmp_path: Path, scope: str
) -> None:
    root = Path.home() if scope == "global" else tmp_path
    path = root / ".yoke" / "config.json"
    path.parent.mkdir(parents=True)
    original = b'{"default_model":"zai:old","tools":{"read":"deny"}}\n'
    path.write_bytes(original)

    with pytest.raises(UnknownModelError) as raised:
        set_default_model("zai:retired", root=tmp_path, repo_scope=scope == "repo")

    assert raised.value.provider_name == "zai"
    assert raised.value.model_id == "retired"
    assert "glm-5.3-flash" in raised.value.available_models
    assert path.read_bytes() == original


def test_empty_catalog_does_not_create_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "yoke.cli.models_app.list_provider_models", lambda *_args, **_kwargs: []
    )

    with pytest.raises(UnknownModelError, match="Available: none"):
        set_default_model("demo:anything", root=tmp_path, repo_scope=True)

    assert not (tmp_path / ".yoke").exists()


def test_arbitrary_model_allowed_without_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "yoke.cli.models_app.list_provider_models", lambda *_args, **_kwargs: None
    )

    path = set_default_model("demo:private-model:high", root=tmp_path, repo_scope=True)

    config = PiConfig.model_validate_json(path.read_text())
    assert config.default_model == "demo:private-model"
    assert config.default_reasoning_effort == "high"
