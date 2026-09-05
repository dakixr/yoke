# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import pytest

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.ai.providers.usage_log import record_provider_usage
from yoke.ai.providers.usage_writer import UsageLogWriteError
from yoke.ai.providers.usage_writer import _write_all
from yoke.ai.providers.usage_writer import append_json_line


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


def _write_usage_records(root: str, count: int) -> None:
    os.environ["YOKE_USAGE_METRIC_LOG_DIR"] = root
    provider = UsageProvider()
    response = provider.complete([], [])
    for _ in range(count):
        record_provider_usage(provider, response)


def test_completion_writes_privacy_safe_usage_metric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))

    response = complete_with_cancel(UsageProvider(), [Message.user("secret")], [])

    assert response.text_content() == "secret response"
    path = next(tmp_path.glob("reported-provider/*.jsonl"))
    raw_line = path.read_text(encoding="utf-8")
    record = json.loads(raw_line)
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
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    provider = UsageProvider()
    message = Message.assistant("response")

    record_provider_usage(provider, message)

    path = next(tmp_path.glob("test-provider/*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["usage_reported"] is False
    assert record["usage"] == {}
    assert record["model"] == "fallback-model"


def test_metric_keeps_schema_one_and_adds_explicit_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    provider = UsageProvider()
    response = provider.complete([], [])

    with usage_metric_context(
        surface="cli",
        session_id="session-1",
        session_title="A local title",
        call_kind="model_iteration",
    ):
        record_provider_usage(provider, response)

    path = next(tmp_path.glob("reported-provider/*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["surface"] == "cli"
    assert record["session_id"] == "session-1"
    assert record["session_title"] == "A local title"
    assert record["call_kind"] == "model_iteration"
    assert "sdk_operation" not in record


def test_post_response_cancellation_keeps_usage_metric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
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

    path = next(tmp_path.glob("reported-provider/*.jsonl"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_concurrent_metrics_are_distinct_json_lines(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    provider = UsageProvider()
    response = provider.complete([], [])

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(lambda _: record_provider_usage(provider, response), range(40))
        )

    path = next(tmp_path.glob("reported-provider/*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 40
    assert len({record["event_id"] for record in records}) == 40


def test_metrics_are_synchronized_across_spawned_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_usage_records, args=(str(tmp_path), 10))
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    path = next(tmp_path.glob("reported-provider/*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 40
    assert len({record["event_id"] for record in records}) == 40


def test_usage_writer_retries_transient_io_failures(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.ai.providers import usage_writer

    attempts = 0
    real_append = usage_writer._append_once

    def flaky_append(path: Path, encoded: bytes) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary failure")
        real_append(path, encoded)

    monkeypatch.setattr(usage_writer, "_append_once", flaky_append)
    monkeypatch.setattr(usage_writer.time, "sleep", lambda _seconds: None)
    path = tmp_path / "usage.jsonl"

    append_json_line(path, {"ok": True})

    assert attempts == 3
    assert json.loads(path.read_text()) == {"ok": True}


def test_usage_writer_raises_after_retry_exhaustion(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.ai.providers import usage_writer

    monkeypatch.setattr(
        usage_writer,
        "_append_once",
        lambda _path, _encoded: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(usage_writer.time, "sleep", lambda _seconds: None)

    with pytest.raises(UsageLogWriteError, match="after 3 attempts"):
        append_json_line(tmp_path / "usage.jsonl", {"ok": True})


def test_usage_writer_does_not_retry_after_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yoke.ai.providers import usage_writer

    writes = 0
    sleeps: list[float] = []

    def partial_write(descriptor: int, encoded: bytes) -> None:
        nonlocal writes
        writes += 1
        os.write(descriptor, encoded[:4])
        raise OSError("append interrupted")

    def failed_rollback(_descriptor: int, _length: int) -> None:
        raise OSError("rollback denied")

    monkeypatch.setattr(usage_writer, "_write_all", partial_write)
    monkeypatch.setattr(usage_writer.os, "ftruncate", failed_rollback)
    monkeypatch.setattr(usage_writer.time, "sleep", sleeps.append)

    with pytest.raises(UsageLogWriteError, match="roll back"):
        append_json_line(tmp_path / "usage.jsonl", {"ok": True})

    assert writes == 1
    assert sleeps == []


def test_usage_writer_closes_file_when_initial_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yoke.ai.providers import usage_writer

    path = tmp_path / "usage.jsonl"
    descriptors: list[int] = []
    real_open, real_stat = os.open, os.fstat

    def tracked_open(target, flags, mode=0o777):
        descriptor = real_open(target, flags, mode)
        if Path(target) == path:
            descriptors.append(descriptor)
        return descriptor

    def failed_stat(descriptor: int):
        if descriptor in descriptors:
            raise OSError("stat failed")
        return real_stat(descriptor)

    monkeypatch.setattr(usage_writer.os, "open", tracked_open)
    monkeypatch.setattr(usage_writer.os, "fstat", failed_stat)
    monkeypatch.setattr(usage_writer.time, "sleep", lambda _delay: None)

    try:
        with pytest.raises(UsageLogWriteError, match="after 3 attempts"):
            append_json_line(path, {"ok": True})
        assert len(descriptors) == 3
        for descriptor in set(descriptors):
            with pytest.raises(OSError):
                real_stat(descriptor)
    finally:
        for descriptor in set(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_usage_writer_handles_partial_writes(monkeypatch) -> None:
    writes: list[bytes] = []

    def partial_write(_descriptor: int, payload: memoryview) -> int:
        chunk = bytes(payload[:2])
        writes.append(chunk)
        return len(chunk)

    monkeypatch.setattr("yoke.ai.providers.usage_writer.os.write", partial_write)

    _write_all(123, b"abcdef")

    assert b"".join(writes) == b"abcdef"


def test_usage_writer_rolls_back_partial_write_before_retry(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.ai.providers import usage_writer

    real_write = usage_writer.os.write
    failed = False

    def partial_then_fail(descriptor: int, payload: memoryview) -> int:
        nonlocal failed
        if not failed:
            failed = True
            real_write(descriptor, payload[:3])
            raise OSError("partial write")
        return real_write(descriptor, payload)

    monkeypatch.setattr(usage_writer.os, "write", partial_then_fail)
    monkeypatch.setattr(usage_writer.time, "sleep", lambda _seconds: None)
    path = tmp_path / "usage.jsonl"

    append_json_line(path, {"ok": True})

    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"ok": True}
    ]


def test_usage_writer_rolls_back_full_write_after_fsync_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.ai.providers import usage_writer

    real_fsync = usage_writer.os.fsync
    failed = False

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(usage_writer.os, "fsync", fail_first_fsync)
    monkeypatch.setattr(usage_writer.time, "sleep", lambda _seconds: None)
    path = tmp_path / "usage.jsonl"

    append_json_line(path, {"ok": True})

    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"ok": True}
    ]


def test_usage_writer_fsyncs_before_return(tmp_path: Path, monkeypatch) -> None:
    from yoke.ai.providers import usage_writer

    descriptors: list[int] = []
    real_fsync = usage_writer.os.fsync

    def tracked_fsync(descriptor: int) -> None:
        descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(usage_writer.os, "fsync", tracked_fsync)

    append_json_line(tmp_path / "usage.jsonl", {"ok": True})

    assert descriptors
