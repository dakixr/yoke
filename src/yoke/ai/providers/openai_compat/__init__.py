"""OpenAI-compatible provider package."""

from yoke.ai.providers.openai_compat.content import (
    normalize_openai_request_messages,
)
from yoke.ai.providers.openai_compat.content import (
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.helpers import build_model_catalog
from yoke.ai.providers.openai_compat.helpers import (
    error_detail as _error_detail,
)
from yoke.ai.providers.openai_compat.helpers import (
    retry_after_seconds as _retry_after_seconds,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleChatCompletionResponse,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleChoice,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleConfig,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleProvider,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleResponseMessage,
)

__all__ = [
    "OpenAICompatibleChatCompletionResponse",
    "OpenAICompatibleChoice",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "OpenAICompatibleResponseMessage",
    "build_model_catalog",
    "_error_detail",
    "_retry_after_seconds",
    "normalize_openai_request_messages",
    "serialize_message_for_openai",
]
