from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai.providers.usage_context import (
    current_usage_metric_context,
)
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.ai.sdk import Agent
from yoke.ai.sdk import BatchTask
from yoke.ai.sdk import RunConfig
from yoke.ai.sdk import complete
from yoke.ai.sdk import run_many


class UsageProvider:
    provider_name = "test-provider"
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        response = Message.assistant("done")
        response.usage = TokenUsage(
            provider_name=self.provider_name,
            model_id="test-model",
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
        )
        return response


class StructuredAnswer(BaseModel):
    answer: int


class StructuredUsageProvider(UsageProvider):
    calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        response = super().complete(messages, tools)
        self.calls += 1
        response.content = "not structured" if self.calls == 1 else '{"answer": 42}'
        return response


def read_records(root: Path) -> list[dict[str, object]]:
    path = next(root.glob("test-provider/*.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_usage_context_merges_and_restores_nested_values() -> None:
    assert current_usage_metric_context().surface is None

    with usage_metric_context(surface="sdk", sdk_operation="agent"):
        with usage_metric_context(call_kind="compaction_summary") as context:
            assert context.surface == "sdk"
            assert context.sdk_operation == "agent"
            assert context.call_kind == "compaction_summary"
        assert current_usage_metric_context().call_kind is None

        with usage_metric_context(
            surface="cli",
            session_id="session-1",
            session_title="Title",
        ) as context:
            assert context.sdk_operation is None
            assert context.surface == "cli"

    assert current_usage_metric_context().surface is None


def test_direct_sdk_completion_records_operation_and_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))

    complete("hello", provider=UsageProvider())

    record = read_records(tmp_path)[0]
    assert record["surface"] == "sdk"
    assert record["sdk_operation"] == "complete"
    assert record["call_kind"] == "direct_completion"
    assert isinstance(record["sdk_run_id"], str)
    assert len(record["sdk_run_id"]) == 32


def test_sdk_agent_records_model_iteration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    agent = Agent(
        provider=UsageProvider(),
        config=RunConfig(root=tmp_path, include_agents_file=False),
    )

    try:
        agent.prompt("hello")
    finally:
        agent.close()

    record = read_records(tmp_path)[0]
    assert record["surface"] == "sdk"
    assert record["sdk_operation"] == "agent"
    assert record["call_kind"] == "model_iteration"
    assert isinstance(record["sdk_run_id"], str)


def test_sdk_agent_marks_structured_output_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    agent = Agent(
        provider=StructuredUsageProvider(),
        config=RunConfig(root=tmp_path, include_agents_file=False),
    )

    try:
        result = agent.prompt("hello", output_type=StructuredAnswer)
    finally:
        agent.close()

    records = read_records(tmp_path)
    assert result.structured == StructuredAnswer(answer=42)
    assert [record["call_kind"] for record in records] == [
        "model_iteration",
        "structured_output_retry",
    ]
    assert len({record["sdk_run_id"] for record in records}) == 1


def test_run_many_preserves_batch_operation_across_async_worker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))

    async def run() -> None:
        await run_many(
            [BatchTask(id="private-task-id", prompt="hello")],
            agent_factory=lambda _task: Agent(
                provider=UsageProvider(),
                config=RunConfig(root=tmp_path, include_agents_file=False),
            ),
        )

    asyncio.run(run())

    record = read_records(tmp_path)[0]
    assert record["surface"] == "sdk"
    assert record["sdk_operation"] == "run_many"
    assert record["call_kind"] == "model_iteration"
    assert "private-task-id" not in json.dumps(record)
