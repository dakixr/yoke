"""Translate the native Yoke provider catalog into ACP model metadata."""

from __future__ import annotations

from collections import Counter
from typing import Any

from yoke.acp.native import NativeClient
from yoke.acp.native import fail


async def discover(
    native: NativeClient,
    directory: str | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return ready provider models and the native default selection."""
    params = {"directory": directory} if directory else {}
    response = await native.request("GET", "provider", params=params)
    providers = response["data"]
    models = await native.data("GET", "model", params=params)
    ready = {
        provider["id"]: provider
        for provider in providers
        if provider.get("ready") is True
    }
    result: dict[str, dict[str, Any]] = {}
    default: str | None = None
    names = Counter(model["name"] for model in models if model["provider"] in ready)
    for model in models:
        provider = ready.get(model["provider"])
        if provider is None:
            continue
        efforts = model["reasoningEfforts"]
        effort = model.get("defaultReasoningEffort")
        if "defaultReasoningEffort" not in model:
            effort = provider.get("currentReasoningEffort")
        if effort not in efforts:
            effort = efforts[0] if efforts else None
        model_id = f"{model['provider']}:{model['id']}"
        result[model_id] = {
            "id": model_id,
            "name": (
                f"{model['name']} ({model['provider']})"
                if names[model["name"]] > 1
                else model["name"]
            ),
            "provider": model["provider"],
            "reasoningEfforts": efforts,
            "defaultReasoningEffort": effort,
            "images": model["capabilities"]["images"],
            "contextWindowTokens": model.get("contextWindowTokens"),
        }
        if default is None and provider.get("currentModel") == model["id"]:
            default = model_id
    if not result:
        raise fail("Native catalog has no models with a ready provider")
    selection = response.get("defaultSelection") or {}
    configured = f"{selection.get('provider')}:{selection.get('model')}"
    if configured in result:
        default = configured
        effort = selection.get("reasoningEffort")
        if effort in result[configured]["reasoningEfforts"]:
            result[configured]["defaultReasoningEffort"] = effort
    return result, default or next(iter(result))
