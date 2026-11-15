"""OpenAI-compatible provider package."""

from yoke.ai.providers.openai_compat.content import (
    normalize_openai_request_messages,
)
from yoke.ai.providers.openai_compat.content import (
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.helpers import build_model_catalog
from yoke.ai.providers.openai_compat.models import (
    OpenAICompatibleChatCompletionResponse,
)
from yoke.ai.providers.openai_compat.models import (
    OpenAICompatibleChoice,
)
from yoke.ai.providers.openai_compat.models import (
    OpenAICompatibleResponseMessage,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleConfig,
)
from yoke.ai.providers.openai_compat.provider import (
    OpenAICompatibleProvider,
)

__all__ = [
    "OpenAICompatibleChatCompletionResponse",
    "OpenAICompatibleChoice",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
    "OpenAICompatibleResponseMessage",
    "build_model_catalog",
    "normalize_openai_request_messages",
    "serialize_message_for_openai",
]
