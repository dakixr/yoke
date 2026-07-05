from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.ai.providers.base import CancellableProvider as CancellableProvider
    from yoke.ai.providers.base import Provider as Provider
    from yoke.ai.providers.base import ProviderCancelledError as ProviderCancelledError
    from yoke.ai.providers.base import ProviderError as ProviderError
    from yoke.ai.providers.base import ProviderRateLimitError as ProviderRateLimitError
    from yoke.ai.providers.base import ProviderServerError as ProviderServerError
    from yoke.ai.providers.codex.subscription import (
        CodexSubscriptionConfig as CodexSubscriptionConfig,
    )
    from yoke.ai.providers.codex.subscription import (
        CodexSubscriptionProvider as CodexSubscriptionProvider,
    )
    from yoke.ai.providers.codex.websockets import CodexWebSockets as CodexWebSockets
    from yoke.ai.providers.codex.websockets import (
        CodexWebSocketsConfig as CodexWebSocketsConfig,
    )
    from yoke.ai.providers.opencode_go import OpenCodeGoConfig as OpenCodeGoConfig
    from yoke.ai.providers.opencode_go import OpenCodeGoProvider as OpenCodeGoProvider
    from yoke.ai.providers.openai_compat import (
        OpenAICompatibleConfig as OpenAICompatibleConfig,
    )
    from yoke.ai.providers.openai_compat import (
        OpenAICompatibleProvider as OpenAICompatibleProvider,
    )
    from yoke.ai.providers.resolution import ProviderReadiness as ProviderReadiness
    from yoke.ai.providers.resolution import ProviderRef as ProviderRef
    from yoke.ai.providers.resolution import (
        available_provider_names as available_provider_names,
    )
    from yoke.ai.providers.resolution import build_provider as build_provider
    from yoke.ai.providers.resolution import (
        is_provider_ready as is_provider_ready,
    )
    from yoke.ai.providers.resolution import (
        list_provider_readiness as list_provider_readiness,
    )
    from yoke.ai.providers.resolution import (
        parse_provider_ref as parse_provider_ref,
    )
    from yoke.ai.providers.resolution import (
        provider_readiness as provider_readiness,
    )
    from yoke.ai.providers.resolution import provider_status as provider_status
    from yoke.ai.providers.zai import ZAIConfig as ZAIConfig
    from yoke.ai.providers.zai import ZAIProvider as ZAIProvider

_LAZY_EXPORTS = {
    "CancellableProvider": ("yoke.ai.providers.base", "CancellableProvider"),
    "Provider": ("yoke.ai.providers.base", "Provider"),
    "ProviderCancelledError": (
        "yoke.ai.providers.base",
        "ProviderCancelledError",
    ),
    "ProviderError": ("yoke.ai.providers.base", "ProviderError"),
    "ProviderRateLimitError": ("yoke.ai.providers.base", "ProviderRateLimitError"),
    "ProviderServerError": ("yoke.ai.providers.base", "ProviderServerError"),
    "CodexSubscriptionConfig": (
        "yoke.ai.providers.codex.subscription",
        "CodexSubscriptionConfig",
    ),
    "CodexSubscriptionProvider": (
        "yoke.ai.providers.codex.subscription",
        "CodexSubscriptionProvider",
    ),
    "CodexWebSockets": ("yoke.ai.providers.codex.websockets", "CodexWebSockets"),
    "CodexWebSocketsConfig": (
        "yoke.ai.providers.codex.websockets",
        "CodexWebSocketsConfig",
    ),
    "OpenCodeGoConfig": ("yoke.ai.providers.opencode_go", "OpenCodeGoConfig"),
    "OpenCodeGoProvider": ("yoke.ai.providers.opencode_go", "OpenCodeGoProvider"),
    "OpenAICompatibleConfig": (
        "yoke.ai.providers.openai_compat",
        "OpenAICompatibleConfig",
    ),
    "OpenAICompatibleProvider": (
        "yoke.ai.providers.openai_compat",
        "OpenAICompatibleProvider",
    ),
    "ProviderReadiness": ("yoke.ai.providers.resolution", "ProviderReadiness"),
    "ProviderRef": ("yoke.ai.providers.resolution", "ProviderRef"),
    "available_provider_names": (
        "yoke.ai.providers.resolution",
        "available_provider_names",
    ),
    "build_provider": ("yoke.ai.providers.resolution", "build_provider"),
    "is_provider_ready": ("yoke.ai.providers.resolution", "is_provider_ready"),
    "list_provider_readiness": (
        "yoke.ai.providers.resolution",
        "list_provider_readiness",
    ),
    "parse_provider_ref": ("yoke.ai.providers.resolution", "parse_provider_ref"),
    "provider_readiness": (
        "yoke.ai.providers.resolution",
        "provider_readiness",
    ),
    "provider_status": ("yoke.ai.providers.resolution", "provider_status"),
    "ZAIConfig": ("yoke.ai.providers.zai", "ZAIConfig"),
    "ZAIProvider": ("yoke.ai.providers.zai", "ZAIProvider"),
}

__all__ = [
    "CodexSubscriptionConfig",
    "CodexSubscriptionProvider",
    "CodexWebSockets",
    "CodexWebSocketsConfig",
    "CancellableProvider",
    "OpenCodeGoConfig",
    "OpenCodeGoProvider",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderCancelledError",
    "ProviderError",
    "ProviderReadiness",
    "ProviderRef",
    "ProviderRateLimitError",
    "ProviderServerError",
    "ZAIConfig",
    "ZAIProvider",
    "available_provider_names",
    "build_provider",
    "is_provider_ready",
    "list_provider_readiness",
    "parse_provider_ref",
    "provider_readiness",
    "provider_status",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve provider package re-exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
