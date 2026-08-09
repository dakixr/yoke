"""Provider-hosted web research helpers."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.base import fork_provider

CODEX_HOSTED_WEB_SEARCH_TOOL: dict[str, object] = {
    "type": "web_search",
    "external_web_access": True,
    "search_context_size": "high",
}

_HOSTED_RESEARCH_PROMPT = """Research the user's question on the web.
Use current, authoritative sources and reconcile conflicting claims. Answer
concisely, include direct source links, and clearly identify uncertainty.
"""


def supports_hosted_web_search(provider: object) -> bool:
    """Return whether the active provider exposes Codex hosted search."""
    return getattr(provider, "provider_name", None) == "codex" and callable(
        getattr(provider, "complete_with_cancel", None)
    )


def hosted_web_research(
    question: str,
    *,
    provider: Provider,
    cancel_requested: Callable[[], bool],
) -> dict[str, object]:
    """Answer a question with the Responses API's hosted web-search tool."""
    if not supports_hosted_web_search(provider):
        raise ValueError("The active provider does not support hosted web search.")

    isolated_provider = fork_provider(provider)
    try:
        _reset_isolated_session(isolated_provider)
        response = complete_with_cancel(
            isolated_provider,
            [
                Message.system(_HOSTED_RESEARCH_PROMPT),
                Message.user(question),
            ],
            [dict(CODEX_HOSTED_WEB_SEARCH_TOOL)],
            cancel_requested=cancel_requested,
        )
        answer = response.plain_text_content
        if not answer:
            raise ProviderError("Codex hosted web search returned no answer.")
        return {
            "ok": True,
            "answer": answer,
            "notes": ["Researched with Codex hosted web search."],
            "sources": [],
            "provider": "codex-hosted",
        }
    finally:
        if isolated_provider is not provider:
            close = getattr(isolated_provider, "close", None)
            if callable(close):
                close()


def _reset_isolated_session(provider: Provider) -> None:
    """Detach a forked hosted request from the parent's response chain."""
    set_session_id = getattr(provider, "set_session_id", None)
    if callable(set_session_id):
        set_session_id(f"hosted-web-{secrets.token_hex(12)}")
