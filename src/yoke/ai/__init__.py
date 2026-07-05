"""Public SDK surface for embedding yoke in Python code."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.agent.compaction import CompactionPolicy as CompactionPolicy
    from yoke.agent.models import Message as Message
    from yoke.agent.models import MessageImageURL as MessageImageURL
    from yoke.agent.models import (
        MessageImageURLContentPart as MessageImageURLContentPart,
    )
    from yoke.agent.models import (
        MessageLocalImageContentPart as MessageLocalImageContentPart,
    )
    from yoke.agent.models import MessageTextContentPart as MessageTextContentPart
    from yoke.ai.providers.base import CancellableProvider as CancellableProvider
    from yoke.ai.providers.base import ProviderCancelledError as ProviderCancelledError
    from yoke.ai.providers.base import ProviderModelInfo as ProviderModelInfo
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
    from yoke.ai.sdk import Agent as Agent
    from yoke.ai.sdk import AgentResult as AgentResult
    from yoke.ai.sdk import CompletionResult as CompletionResult
    from yoke.ai.sdk import Context as Context
    from yoke.ai.sdk import ConversationEntryHistory as ConversationEntryHistory
    from yoke.ai.sdk import ConversationHistory as ConversationHistory
    from yoke.ai.sdk import Image as Image
    from yoke.ai.sdk import MessageHistory as MessageHistory
    from yoke.ai.sdk import RunConfig as RunConfig
    from yoke.ai.sdk import Skill as Skill
    from yoke.ai.sdk import StructuredOutputError as StructuredOutputError
    from yoke.ai.sdk import complete as complete
    from yoke.ai.sdk.helpers import build_user_message as build_user_message
    from yoke.ai.sdk.helpers import image_part as image_part
    from yoke.ai.sdk.helpers import remote_image_part as remote_image_part
    from yoke.ai.sdk.helpers import text_part as text_part
    from yoke.observe import step as step
    from yoke.observe import workflow as workflow

_LAZY_EXPORTS = {
    "CompactionPolicy": ("yoke.agent.compaction", "CompactionPolicy"),
    "Message": ("yoke.agent.models", "Message"),
    "MessageImageURL": ("yoke.agent.models", "MessageImageURL"),
    "MessageImageURLContentPart": (
        "yoke.agent.models",
        "MessageImageURLContentPart",
    ),
    "MessageLocalImageContentPart": (
        "yoke.agent.models",
        "MessageLocalImageContentPart",
    ),
    "MessageTextContentPart": ("yoke.agent.models", "MessageTextContentPart"),
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
    "CancellableProvider": ("yoke.ai.providers.base", "CancellableProvider"),
    "ProviderCancelledError": (
        "yoke.ai.providers.base",
        "ProviderCancelledError",
    ),
    "ProviderModelInfo": ("yoke.ai.providers.base", "ProviderModelInfo"),
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
    "Agent": ("yoke.ai.sdk", "Agent"),
    "AgentResult": ("yoke.ai.sdk", "AgentResult"),
    "CompletionResult": ("yoke.ai.sdk", "CompletionResult"),
    "ConversationEntryHistory": ("yoke.ai.sdk", "ConversationEntryHistory"),
    "ConversationHistory": ("yoke.ai.sdk", "ConversationHistory"),
    "Context": ("yoke.ai.sdk", "Context"),
    "Image": ("yoke.ai.sdk", "Image"),
    "MessageHistory": ("yoke.ai.sdk", "MessageHistory"),
    "RunConfig": ("yoke.ai.sdk", "RunConfig"),
    "Skill": ("yoke.ai.sdk", "Skill"),
    "StructuredOutputError": ("yoke.ai.sdk", "StructuredOutputError"),
    "complete": ("yoke.ai.sdk", "complete"),
    "build_user_message": ("yoke.ai.sdk.helpers", "build_user_message"),
    "image_part": ("yoke.ai.sdk.helpers", "image_part"),
    "remote_image_part": ("yoke.ai.sdk.helpers", "remote_image_part"),
    "text_part": ("yoke.ai.sdk.helpers", "text_part"),
    "step": ("yoke.observe", "step"),
    "workflow": ("yoke.observe", "workflow"),
}

__all__ = [
    "Agent",
    "AgentResult",
    "CodexSubscriptionConfig",
    "CodexSubscriptionProvider",
    "CodexWebSockets",
    "CodexWebSocketsConfig",
    "CompletionResult",
    "CancellableProvider",
    "ConversationEntryHistory",
    "ConversationHistory",
    "CompactionPolicy",
    "Context",
    "Image",
    "Message",
    "MessageHistory",
    "MessageImageURL",
    "MessageImageURLContentPart",
    "MessageLocalImageContentPart",
    "MessageTextContentPart",
    "OpenCodeGoConfig",
    "OpenCodeGoProvider",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "ProviderModelInfo",
    "ProviderCancelledError",
    "ProviderReadiness",
    "ProviderRef",
    "RunConfig",
    "Skill",
    "StructuredOutputError",
    "ZAIConfig",
    "ZAIProvider",
    "build_user_message",
    "available_provider_names",
    "build_provider",
    "complete",
    "image_part",
    "is_provider_ready",
    "list_provider_readiness",
    "parse_provider_ref",
    "provider_readiness",
    "provider_status",
    "remote_image_part",
    "step",
    "text_part",
    "workflow",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve SDK exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
