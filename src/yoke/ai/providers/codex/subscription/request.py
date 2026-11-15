"""Pure Codex request construction over explicit configuration and state."""

from __future__ import annotations

import platform
import secrets

from yoke.agent.models import Message

from .catalog import (
    X_CODEX_TURN_STATE_HEADER,
    X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER,
    originator_for_model,
    uses_responses_lite,
)
from .config import CodexSubscriptionConfig
from .helpers import clamp_reasoning_effort
from .messages import convert_messages, convert_tools
from .models import OAuthCredentials


def request_payload(
    messages: list[Message],
    tools: list[dict[str, object]],
    *,
    config: CodexSubscriptionConfig,
    prompt_cache_key: str,
    turn_state: str | None,
) -> dict[str, object]:
    instructions, input_items = convert_messages(messages)
    responses_lite = uses_responses_lite(config.model)
    client_metadata: dict[str, object] = {}
    if turn_state:
        client_metadata[X_CODEX_TURN_STATE_HEADER] = turn_state
    payload: dict[str, object] = {
        "model": config.model,
        "store": False,
        "stream": True,
        "input": input_items,
        "text": {"verbosity": config.text_verbosity},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": prompt_cache_key,
        "tool_choice": "auto",
        "parallel_tool_calls": not responses_lite,
        "reasoning": {
            "effort": clamp_reasoning_effort(config.model, config.reasoning_effort),
            "summary": "auto",
            **({"context": "all_turns"} if responses_lite else {}),
        },
    }
    if instructions:
        payload["instructions"] = instructions
    if tools:
        payload["tools"] = convert_tools(tools)
    if client_metadata:
        payload["client_metadata"] = client_metadata
    return payload


def request_headers(
    credentials: OAuthCredentials,
    *,
    config: CodexSubscriptionConfig,
    prompt_cache_key: str,
    turn_state: str | None,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.access}",
        "chatgpt-account-id": credentials.account_id,
        "originator": originator_for_model(config.model, config.originator),
        "User-Agent": (
            f"yoke ({platform.system().lower()}; {platform.machine().lower()})"
        ),
        "OpenAI-Beta": "responses=experimental",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        # Codex backends and codex-lb derive cache and account affinity
        # from session_id; a per-request value scatters the prompt cache.
        "session_id": prompt_cache_key,
        "x-client-request-id": secrets.token_hex(16),
        **({X_CODEX_TURN_STATE_HEADER: turn_state} if turn_state else {}),
        **(
            {X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER: "true"}
            if uses_responses_lite(config.model)
            else {}
        ),
    }


def hosted_image_payload(
    *,
    prompt: str,
    reference_image_urls: list[str],
    config: CodexSubscriptionConfig,
    prompt_cache_key: str,
) -> dict[str, object]:
    content: list[dict[str, object]] = [
        {"type": "input_text", "text": prompt},
    ]
    content.extend(
        {"type": "input_image", "image_url": image_url}
        for image_url in reference_image_urls
    )
    return {
        "model": config.model,
        "store": False,
        "stream": True,
        "instructions": (
            "Use the hosted image_generation tool to generate exactly one PNG "
            "image for the user's prompt."
        ),
        "input": [{"role": "user", "content": content}],
        "tools": [{"type": "image_generation", "output_format": "png"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {
            "effort": clamp_reasoning_effort(config.model, config.reasoning_effort),
            "summary": "auto",
        },
        "text": {"verbosity": config.text_verbosity},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": prompt_cache_key,
    }
