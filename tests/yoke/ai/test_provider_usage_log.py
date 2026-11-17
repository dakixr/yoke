# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar, cast

import pytest

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.ai.providers.usage_log import record_provider_usage
from yoke.ai.providers.usage_writer import UsageLogWriteError
from yoke.ai.providers.usage_writer import append_usage, read_usage_events


class UsageProvider:
    provider_name = "test-provider"
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def current_model_id(self) -> str:
        return "fallback-model"

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        response = Message.assistant("secret response")
        response.usage = TokenUsage(
            provider_name="reported-provider",
            model_id="reported-model",
            input_tokens=100,
            cached_input_tokens=80,
            cache_creation_input_tokens=15,
            output_tokens=20,
            reasoning_tokens=5,
            total_tokens=120,
            raw={"prompt": "secret prompt", "api_key": "secret key"},
        )
        return response


def test_completion_writes_privacy_safe_usage_metric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "yoke.db"))

    response = complete_with_cancel(UsageProvider(), [Message.user("secret")], [])

    assert response.text_content() == "secret response"
    record = next(read_usage_events())
    raw_line = json.dumps(record)
    assert record["provider"] == "reported-provider"
    assert record["model"] == "reported-model"
    assert record["usage"] == {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "cache_creation_input_tokens": 15,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "total_tokens": 120,
    }
    assert "secret" not in raw_line
    assert "raw" not in raw_line


def test_missing_usage_still_writes_completed_provider_metric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "yoke.db"))
    provider = UsageProvider()
    message = Message.assistant("response")

    record_provider_usage(provider, message)

    record = next(read_usage_events())
    assert record["usage_reported"] is False
    assert record["usage"] == {}
    assert record["model"] == "fallback-model"


def test_metric_keeps_schema_one_and_adds_explicit_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "yoke.db"))
    provider = UsageProvider()
    response = provider.complete([], [])

    with usage_metric_context(
        surface="cli",
        session_id="session-1",
        session_title="A local title",
        call_kind="model_iteration",
    ):
        record_provider_usage(provider, response)

    record = next(read_usage_events())
    assert record["schema_version"] == 1
    assert record["surface"] == "cli"
    assert record["session_id"] == "session-1"
    assert record["session_title"] == "A local title"
    assert record["call_kind"] == "model_iteration"
    assert "sdk_operation" not in record


def test_post_response_cancellation_keeps_usage_metric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "yoke.db"))
    checks = iter((False, True))

    try:
        complete_with_cancel(
            UsageProvider(),
            [Message.user("secret")],
            [],
            cancel_requested=lambda: next(checks),
        )
    except ProviderCancelledError:
        pass
    else:
        raise AssertionError("Expected post-response cancellation")

    assert len(list(read_usage_events())) == 1


def test_concurrent_metrics_are_distinct_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "yoke.db"))
    provider = UsageProvider()
    response = provider.complete([], [])

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(lambda _: record_provider_usage(provider, response), range(40))
        )

    records = list(read_usage_events(database_path=tmp_path / "yoke.db"))
    assert len(records) == 40
    assert len({record["event_id"] for record in records}) == 40


def test_usage_transaction_rolls_back_on_failure(tmp_path: Path, monkeypatch) -> None:
    import sqlite3
    from yoke.session import SessionStore

    store = SessionStore()
    with store.database.write() as connection:
        connection.execute(
            "CREATE TRIGGER fail_usage BEFORE INSERT ON usage_events BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
    with pytest.raises(UsageLogWriteError, match="Could not persist") as caught:
        record_provider_usage(UsageProvider(), UsageProvider().complete([], []))
    assert isinstance(caught.value.__cause__, sqlite3.IntegrityError)
    assert list(read_usage_events()) == []
    with store.database.write() as connection:
        connection.execute("DROP TRIGGER fail_usage")
    record_provider_usage(UsageProvider(), UsageProvider().complete([], []))
    assert len(list(read_usage_events())) == 1


def test_duplicate_event_is_reported_without_partial_insert() -> None:
    record_provider_usage(UsageProvider(), UsageProvider().complete([], []))
    record = next(read_usage_events())
    with pytest.raises(UsageLogWriteError):
        append_usage(record)
    assert list(read_usage_events()) == [record]


def test_usage_reporting_filters_keep_original_schema() -> None:
    with usage_metric_context(surface="cli", session_id="one"):
        record_provider_usage(UsageProvider(), UsageProvider().complete([], []))
    with usage_metric_context(surface="sdk", session_id="two"):
        record_provider_usage(UsageProvider(), Message.assistant("no usage"))
    first = list(read_usage_events(provider="reported-provider", session_id="one"))
    assert len(first) == 1
    assert first[0]["schema_version"] == 1
    usage = cast(dict[str, object], first[0]["usage"])
    assert usage["cached_input_tokens"] == 80
    assert list(read_usage_events(since="9999")) == []
