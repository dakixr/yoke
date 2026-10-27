"""Real provider inventories projected through HTTP and ACP without inference."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.catalog import discover
from yoke.acp.native import NativeClient
from yoke.http.app import HttpAppSettings, create_app


@pytest.mark.parametrize("workspace_default", [False, True])
def test_complete_ready_inventory_defaults_and_reasoning(
    tmp_path, monkeypatch, workspace_default
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for key in tuple(os.environ):
        if key.startswith(("YOKE_", "OPENCODE_", "ZAI_")):
            monkeypatch.delenv(key)
    for key in ("YOKE_CODEX_API_KEY", "OPENCODE_API_KEY", "ZAI_API_KEY"):
        monkeypatch.setenv(key, "catalog-fixture-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def scenario():
        app = create_app(
            HttpAppSettings(
                auth_token="catalog-secret", session_directory=tmp_path / "sessions"
            )
        )
        native = NativeClient("http://fixture", "catalog-secret")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer catalog-secret"},
        )
        try:
            params = {"directory": str(workspace)}
            providers = await native.data("GET", "provider", params=params)
            ready = {provider["id"] for provider in providers if provider["ready"]}
            models = await native.data("GET", "model", params=params)
            expected = {
                f"{model['provider']}:{model['id']}": model
                for model in models
                if model["provider"] in ready
            }
            assert expected

            def configure_default(directory, provider):
                selected_id, selected_model = next(
                    (model_id, model)
                    for model_id, model in expected.items()
                    if model["provider"] == provider
                )
                efforts = selected_model["reasoningEfforts"]
                selected_effort = efforts[-1] if efforts else None
                config = directory / ".yoke/config.json"
                config.parent.mkdir(parents=True)
                config.write_text(
                    json.dumps(
                        {
                            "default_model": selected_id,
                            "default_reasoning_effort": selected_effort,
                        }
                    )
                )
                return selected_id, selected_effort

            selected_id, selected_effort = configure_default(tmp_path / "home", "codex")
            if workspace_default:
                selected_id, selected_effort = configure_default(workspace, "zai")
            catalog, default = await discover(native, str(workspace))
            assert set(catalog) == set(expected)
            assert default == selected_id
            assert len({model["name"] for model in catalog.values()}) == len(expected)
            agent = YokeAcpAgent(native)
            for model_id, model in expected.items():
                efforts = model["reasoningEfforts"]
                expected_default = (
                    selected_effort
                    if model_id == selected_id
                    else model["defaultReasoningEffort"]
                )
                assert catalog[model_id]["reasoningEfforts"] == efforts
                assert catalog[model_id]["defaultReasoningEffort"] == expected_default
                assert catalog[model_id]["images"] == model["capabilities"]["images"]
                assert (
                    catalog[model_id]["contextWindowTokens"]
                    == model["contextWindowTokens"]
                )
                agent.remember(
                    "fixture",
                    {
                        "selection": {
                            "provider": model_id.split(":")[0],
                            "model": model_id.split(":")[1],
                            "reasoningEffort": expected_default,
                        }
                    },
                    catalog,
                    default,
                )
                options = {option["id"]: option for option in agent.options("fixture")}
                assert {item["value"] for item in options["model"]["options"]} == set(
                    expected
                )
                if efforts:
                    assert [
                        item["value"] for item in options["reasoningEffort"]["options"]
                    ] == efforts
                    assert (
                        options["reasoningEffort"]["currentValue"] == expected_default
                    )
                else:
                    assert "reasoningEffort" not in options
            # Removing a credential hides that provider, without hiding its siblings.
            monkeypatch.delenv("ZAI_API_KEY")
            app.state.catalog_service._readiness_cache = None
            catalog, default = await discover(native, str(workspace))
            assert set(catalog) == {
                model_id
                for model_id, model in expected.items()
                if model["provider"] != "zai"
            }
            assert default in catalog
        finally:
            await native.close()

    asyncio.run(scenario())
