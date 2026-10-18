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


EXPECTED = {
    "codex:gpt-6-astra": (["low", "medium", "high", "xhigh", "max"], "medium"),
    "codex:gpt-6-luna": (["none", "low", "medium", "high", "xhigh", "max"], "medium"),
    "codex:gpt-6.1-sol": (["low", "medium", "high", "xhigh", "max"], "medium"),
    "opencode-go:deepseek-v4.1-flash": (["low", "high", "max"], "high"),
    "opencode-go:glm-5.3-flash": ([], None),
    "opencode-go:muse-spark-1.3-contributor": (
        ["minimal", "low", "medium", "high", "xhigh"],
        "high",
    ),
    "zai:glm-5.3-flash": (["low", "high", "max"], "max"),
}


@pytest.mark.parametrize("workspace_default", [None, "zai:glm-5.3-flash"])
def test_complete_ready_inventory_defaults_and_reasoning(
    tmp_path, monkeypatch, workspace_default
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for key in tuple(os.environ):
        if key.startswith(("YOKE_", "OPENCODE_", "ZAI_")):
            monkeypatch.delenv(key)
    for key in ("YOKE_CODEX_API_KEY", "OPENCODE_API_KEY", "ZAI_API_KEY"):
        monkeypatch.setenv(key, "catalog-fixture-key")
    global_config = tmp_path / "home/.yoke/config.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(
        json.dumps(
            {"default_model": "codex:gpt-6.1-sol", "default_reasoning_effort": "medium"}
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if workspace_default:
        config = workspace / ".yoke/config.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {"default_model": workspace_default, "default_reasoning_effort": "high"}
            )
        )

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
            catalog, default = await discover(native, str(workspace))
            assert set(catalog) == set(EXPECTED)
            assert default == (workspace_default or "codex:gpt-6.1-sol")
            assert len({model["name"] for model in catalog.values()}) == len(EXPECTED)
            agent = YokeAcpAgent(native)
            for model_id, (efforts, model_default) in EXPECTED.items():
                expected_default = (
                    "high" if workspace_default == model_id else model_default
                )
                assert catalog[model_id]["reasoningEfforts"] == efforts
                assert catalog[model_id]["defaultReasoningEffort"] == expected_default
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
                    EXPECTED
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
            assert set(catalog) == set(EXPECTED) - {"zai:glm-5.3-flash"}
            assert default in catalog
        finally:
            await native.close()

    asyncio.run(scenario())
