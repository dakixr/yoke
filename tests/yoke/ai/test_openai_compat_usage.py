# ruff: noqa: D100, D103, S101

from __future__ import annotations

from yoke.agent.models import Message
from yoke.agent.models import MessageTextContentPart
from yoke.ai.providers.openai_compat.content import (
    serialize_message_for_openai,
)
from yoke.ai.providers.usage import parse_token_usage


def test_usage_parses_anthropic_cache_fields() -> None:
    usage = parse_token_usage(
        {
            "input_tokens": 10,
            "completion_tokens": 10,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 20,
            "total_tokens": 110,
        },
        provider_name="claude",
        model_id="claude-haiku-4-5",
    )

    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 10
    assert usage.cached_input_tokens == 70
    assert usage.cache_creation_input_tokens == 20


def test_usage_parses_litellm_cache_creation_details() -> None:
    usage = parse_token_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {
                "cached_tokens": 70,
                "cache_creation_tokens": 20,
            },
        }
    )

    assert usage is not None
    assert usage.cached_input_tokens == 70
    assert usage.cache_creation_input_tokens == 20


def test_openai_serializer_preserves_text_cache_control() -> None:
    payload = serialize_message_for_openai(
        Message.user(
            [
                MessageTextContentPart(
                    text="stable prompt",
                    cache_control={"type": "ephemeral"},
                ),
                MessageTextContentPart(text="fresh prompt"),
            ]
        )
    )

    assert payload["content"] == [
        {
            "type": "text",
            "text": "stable prompt",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "fresh prompt"},
    ]
