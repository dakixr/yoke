"""Lazy public exports; generate the matching stub with docs/tools/generate_exports.py."""

from __future__ import annotations
from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "CodexSubscriptionConfig": "yoke.ai.providers.codex.subscription",
    "CodexSubscriptionProvider": "yoke.ai.providers.codex.subscription",
    "CodexWebSockets": ("yoke.ai.providers.codex.websocket.provider", "CodexProvider"),
    "CodexWebSocketsConfig": (
        "yoke.ai.providers.codex.websocket.config",
        "CodexConfig",
    ),
    "CancellableProvider": "yoke.ai.providers.base",
    "OpenCodeGoConfig": "yoke.ai.providers.opencode_go",
    "OpenCodeGoProvider": "yoke.ai.providers.opencode_go",
    "OpenAICompatibleConfig": "yoke.ai.providers.openai_compat",
    "OpenAICompatibleProvider": "yoke.ai.providers.openai_compat",
    "Provider": "yoke.ai.providers.base",
    "ProviderCancelledError": "yoke.ai.providers.base",
    "ProviderError": "yoke.ai.providers.base",
    "ProviderReadiness": "yoke.ai.providers.resolution",
    "ProviderRef": "yoke.ai.providers.resolution",
    "ProviderRateLimitError": "yoke.ai.providers.base",
    "ProviderServerError": "yoke.ai.providers.base",
    "UsageLogWriteError": "yoke.ai.providers.usage_writer",
    "ZAIConfig": "yoke.ai.providers.zai",
    "ZAIProvider": "yoke.ai.providers.zai",
    "available_provider_names": "yoke.ai.providers.resolution",
    "build_provider": "yoke.ai.providers.resolution",
    "is_provider_ready": "yoke.ai.providers.resolution",
    "list_provider_readiness": "yoke.ai.providers.resolution",
    "parse_provider_ref": "yoke.ai.providers.resolution",
    "provider_readiness": "yoke.ai.providers.resolution",
    "provider_status": "yoke.ai.providers.resolution",
}

__all__ = list(_LAZY_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> Any:
    """Resolve an export on first access, retaining normal module identity."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = (target, name) if isinstance(target, str) else target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
