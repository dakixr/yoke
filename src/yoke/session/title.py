"""Shared LLM-generated session titles."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.cache_scope import conversation_cache_scope
from yoke.agent.models import Message
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderRequestContext
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.base import fork_provider
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.session import fallback_session_title


LOGGER = logging.getLogger(__name__)
SESSION_TITLE_PROMPT = (
    "Create a concise title of no more than 6 words for this conversation. "
    "Do not explain your reasoning. Return only the title, without quotes, "
    "markdown, or punctuation at the end."
)
SESSION_TITLE_RETRY_PROMPT = "Return only the 6-word-or-shorter title now."


def generate_session_title(
    agent: object,
    messages: Sequence[Message],
) -> str | None:
    """Generate a title from a conversation without changing its transcript."""
    provider = getattr(agent, "provider", None)
    if provider is None or not messages:
        return None
    provider, request_messages, tools, cache_scope = build_session_title_request(
        agent, messages, provider
    )
    for attempt in range(2):
        try:
            with usage_metric_context(call_kind="session_title"):
                response = complete_with_cancel(
                    provider,
                    request_messages,
                    tools,
                    request_context=ProviderRequestContext(
                        cache_scope=cache_scope,
                        response_continuity="isolated",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Session title generation failed: %s", exc)
            return None
        title = normalize_generated_title(response.plain_text_content)
        if title:
            return title
        if attempt == 0:
            request_messages.append(Message.user(SESSION_TITLE_RETRY_PROMPT))
    LOGGER.warning("Session title generation returned no text.")
    return None


def build_session_title_request(
    agent: object,
    messages: Sequence[Message],
    provider: Provider,
) -> tuple[Provider, list[Message], list[dict[str, object]], str]:
    """Append a title instruction to the canonical provider request."""
    tools: list[dict[str, object]] = []
    cache_scope = "empty-conversation"
    provider_messages = [message.model_copy(deep=True) for message in messages]
    if isinstance(agent, RuntimeAgent):
        context = agent._context
        if context is None or agent.messages != list(messages):
            context = agent.context_manager.initialize(
                "",
                list(messages),
                append_prompt=False,
                available_skills=agent.available_skills,
                active_skills=agent.active_skills,
            )
        provider_messages = messages_for_provider_capabilities(
            agent.context_manager.messages_for_provider(context), provider
        )
        tools = agent._tool_definitions()
        cache_scope = conversation_cache_scope(context)
    provider_messages.append(Message.user(SESSION_TITLE_PROMPT))
    return fork_provider(provider), provider_messages, tools, cache_scope


def normalize_generated_title(content: str | None) -> str | None:
    """Normalize a model response into one short title."""
    if not content:
        return None
    first_line = next(
        (line.strip() for line in content.splitlines() if line.strip()), ""
    )
    normalized = first_line.strip("\"'`#* ").rstrip(".!?:;, ")
    words = normalized.split()
    if not words:
        return None
    return fallback_session_title(" ".join(words[:6]))
