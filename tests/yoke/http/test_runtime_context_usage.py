from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.http.services.runtime_context_usage import provider_response_context_usage
from yoke.http.services.runtime_context_usage import public_context_usage
from yoke.http.services.runtime_start import RuntimeAppendPersistence
from yoke.session import SessionStore


def test_public_context_usage_allows_accounting_but_not_arbitrary_fields() -> None:
    projected = public_context_usage(
        {
            "input_tokens": 12_000,
            "max_total_tokens": 100_000,
            "usage_percent": 12,
            "reason": "tool_results",
            "api_key": "secret",
            "nested": {"token": "secret"},
        }
    )
    assert projected == {
        "input_tokens": 12_000,
        "max_total_tokens": 100_000,
        "usage_percent": 12,
        "reason": "tool_results",
    }


def test_provider_response_usage_uses_existing_reported_counts() -> None:
    projected = provider_response_context_usage(
        {
            "iteration": 3,
            "usage": {
                "input_tokens": 64_000,
                "output_tokens": 1_200,
                "reasoning_tokens": 900,
                "total_tokens": 65_200,
                "cached_input_tokens": 48_000,
                "cache_creation_input_tokens": 3_000,
            },
        },
        max_total_tokens=200_000,
    )
    assert projected == {
        "reason": "provider_response",
        "input_tokens": 64_000,
        "provider_reported_input_tokens": 64_000,
        "max_total_tokens": 200_000,
        "usage_percent": 32,
        "accounting_source": "provider",
        "iteration": 3,
        "output_tokens": 1_200,
        "reasoning_tokens": 900,
        "provider_reported_total_tokens": 65_200,
        "cached_input_tokens": 48_000,
        "cache_creation_input_tokens": 3_000,
    }


def test_runtime_append_preserves_latest_context_usage(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    record = store.save("session", [Message.user("start")], root=tmp_path)
    usage: dict[str, object] = {
        "input_tokens": 64_000,
        "max_total_tokens": 200_000,
        "usage_percent": 32,
    }
    record = store.set_context_usage("session", usage, existing_record=record)
    assert record.leaf_id is not None
    entry = ConversationEntry(
        kind="assistant",
        message=Message.assistant("checkpoint"),
        parent_id=record.leaf_id,
    )
    persistence = RuntimeAppendPersistence(
        runtime_entry_count=0,
        leaf_id=record.leaf_id,
    )

    persisted = persistence.append(
        store,
        "session",
        [entry],
        input_id=None,
        active_skills=None,
    )

    assert persisted.context_usage == usage
    summary = store.summary_record("session")
    assert summary is not None
    assert summary.context_usage == usage
