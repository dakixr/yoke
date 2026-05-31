# ruff: noqa: D100,D103,S101

from __future__ import annotations

from pathlib import Path

from yoke.cli.providers import list_custom_provider_models


def test_list_custom_provider_models_uses_plugin_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.providers.registry.Path.home", lambda: home)
    provider_dir = home / ".yoke" / "providers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "demo.py").write_text(
        "from yoke.ai.providers.base import ProviderModelInfo\n"
        "from yoke.ai.providers.openai_compat import (\n"
        "    OpenAICompatibleConfig,\n"
        "    OpenAICompatibleProvider,\n"
        ")\n"
        "PROVIDER_NAME = 'demo'\n"
        "def list_provider_models(context):\n"
        "    del context\n"
        "    return [\n"
        "        ProviderModelInfo(\n"
        "            id='demo-model',\n"
        "            display_name='Demo Model',\n"
        "            context_window_tokens=12345,\n"
        "            thinking_levels=('low', 'medium'),\n"
        "            supports_image_inputs=True,\n"
        "        )\n"
        "    ]\n"
        "def register_provider(context):\n"
        "    del context\n"
        "    return OpenAICompatibleProvider(\n"
        "        OpenAICompatibleConfig(\n"
        "            api_key='k',\n"
        "            model='demo-model',\n"
        "            provider_name=PROVIDER_NAME,\n"
        "            model_catalog=tuple(list_provider_models(None)),\n"
        "        )\n"
        "    )\n",
        encoding="utf-8",
    )

    catalog = list_custom_provider_models("demo", home=home)

    assert catalog is not None
    assert len(catalog) == 1
    assert catalog[0].id == "demo-model"
    assert catalog[0].context_window_tokens == 12345
