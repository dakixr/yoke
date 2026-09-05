# ruff: noqa: D100, D103, S101

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"output_tokens": 0, "completion_tokens": 7}, 0),
        ({"output_tokens": 3, "completion_tokens": 7}, 3),
        ({"completion_tokens": 7}, 7),
    ],
)
def test_usage_output_token_alias_preserves_precedence(
    raw: dict[str, int], expected: int
) -> None:
    usage = parse_token_usage(raw)

    assert usage is not None
    assert usage.output_tokens == expected


@pytest.mark.parametrize(
    ("top_level", "expected_input", "expected_creation"),
    [(0, 10, 0), (2, 12, 2), (None, 17, 7)],
)
def test_usage_cache_creation_preserves_top_level_precedence(
    top_level: int | None,
    expected_input: int,
    expected_creation: int,
) -> None:
    raw: dict[str, object] = {
        "input_tokens": 10,
        "input_tokens_details": {"cache_creation_tokens": 7},
    }
    if top_level is not None:
        raw["cache_creation_input_tokens"] = top_level

    usage = parse_token_usage(raw)

    assert usage is not None
    assert usage.input_tokens == expected_input
    assert usage.cache_creation_input_tokens == expected_creation


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
