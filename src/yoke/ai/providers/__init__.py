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
    "ProviderRateLimitError",
    "ProviderServerError",
    "ZAIConfig",
    "ZAIProvider",
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
