"""Exercise real SDK loops with and without tracking at the provider boundary."""

from __future__ import annotations

from collections import deque
from contextlib import nullcontext
from copy import deepcopy

from pydantic import BaseModel
import pytest

from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT, CompactionPolicy
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools import LocalTool
from yoke.agent_runs import AgentRunRegistry
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.usage_context import current_usage_metric_context
from yoke.ai.sdk import Agent, Image, RunConfig, Skill


class Answer(BaseModel):
    answer: int


class Evidence(LocalTool):
    name = "evidence"
    description = "Read a fixed test fixture."
    execute_in_process = True

    def execute(self):
        return {"ok": True, "evidence": "fixture says 42"}


def tool_call():
    return Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(
                id="evidence-call",
                function=ToolFunction(name="evidence", arguments="{}"),
            )
        ],
    )


class RecordingProvider:
    provider_name = "fixture"
    max_images_per_message = None

    def __init__(self, responses=(), *, images=True):
        self.supports_image_inputs = images
        self.responses = deque(responses)
        self.requests = []

    def complete(self, messages, tools):
        raise AssertionError("Runtime must supply cache and continuity context")

    def complete_with_context(self, messages, tools, *, request_context):
        self.requests.append(
            (
                [message.model_dump() for message in messages],
                deepcopy(tools),
                request_context,
                current_usage_metric_context().call_kind,
            )
        )
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response.model_copy(deep=True)


@pytest.fixture
def registry():
    registry = AgentRunRegistry()
    try:
        yield registry
    finally:
        registry.close()


def make_seed(tmp_path, config):
    """Load both variants from one checkpoint, preserving real cache identity."""
    seed = Agent(provider=RecordingProvider(), config=config)
    try:
        return seed.save(tmp_path / "seed.json")
    finally:
        seed.close()


@pytest.mark.parametrize("images", [True, False], ids=["vision", "text-only"])
def test_tool_skill_history_image_fork_and_structured_retry_projection(
    registry, tmp_path, images
):
    image = Image.from_url("data:image/png;base64,aW1hZ2U=")
    config = RunConfig(
        root=tmp_path,
        include_agents_file=False,
        sys_prompt="Report only the evidence.",
        skills=[Skill.inline("review", "Check the fixture before answering.")],
        tools=[Evidence],
        tool_execution="sequential",
        messages=[Message.user([image.content]), Message.assistant("Stored image.")],
    )
    seed = make_seed(tmp_path, config)
    requests = []
    for tracked in (False, True):
        provider = RecordingProvider(
            [
                tool_call(),
                Message.assistant("not JSON"),
                Message.assistant('{"answer":42}'),
                Message.assistant("branch"),
                Message.assistant("parent followup"),
            ],
            images=images,
        )
        with registry.bind(session_id="owner") if tracked else nullcontext():
            agent = Agent.load(seed, provider=provider, config=config)
            fork = None
            try:
                result = agent.prompt(
                    "Read the evidence.",
                    output_type=Answer,
                    images=[image] if images else [],
                )
                assert result.structured == Answer(answer=42)
                fork = agent.fork()
                assert fork.prompt("Explore the branch.").output == "branch"
                assert agent.prompt("Continue the parent.").output == "parent followup"
                assert "Explore the branch." not in str(provider.requests[-1][0])
            finally:
                if fork is not None:
                    fork.close()
                agent.close()
        assert not provider.responses
        assert len(provider.requests) == 5
        first_messages, tools, _, _ = provider.requests[0]
        assert "Check the fixture before answering." in str(first_messages)
        assert any(tool["function"]["name"] == "evidence" for tool in tools)
        assert ("image_url" in str(first_messages)) == images
        second_messages = provider.requests[1][0]
        assert any(m["tool_call_id"] == "evidence-call" for m in second_messages)
        assert "fixture says 42" in str(second_messages)
        assert provider.requests[2][3] == "structured_output_retry"
        assert "adhere exactly to the schema" in str(provider.requests[2][0])
        assert "adhere exactly to the schema" not in str(provider.requests[3][0])
        assert len({request[2].cache_scope for request in provider.requests}) == 1
        requests.append(provider.requests)
    assert requests[0] == requests[1]
    assert len(registry.snapshots()) == 3
    assert {row["status"] for row in registry.snapshots()} == {"completed"}


def test_tool_checkpoint_recovery_projection(registry, tmp_path):
    config = RunConfig(
        root=tmp_path,
        include_agents_file=False,
        tools=[Evidence],
        tool_execution="sequential",
        messages=[
            Message.user("earlier request"),
            Message.assistant("earlier response"),
        ],
    )
    seed = make_seed(tmp_path, config)
    requests = []
    for tracked in (False, True):
        checkpoint = tmp_path / f"checkpoint-{tracked}.json"
        checkpoint.write_bytes(seed.read_bytes())
        provider = RecordingProvider([tool_call(), ProviderError("fixture failure")])
        with registry.bind(session_id="owner") if tracked else nullcontext():
            agent = Agent.load(
                checkpoint, provider=provider, config=config, autosave=True
            )
            try:
                with pytest.raises(ProviderError, match="fixture failure"):
                    agent.prompt("Read then stop.")
            finally:
                agent.close()
            resumed_provider = RecordingProvider([Message.assistant("resumed")])
            resumed = Agent.load(checkpoint, provider=resumed_provider, config=config)
            try:
                assert resumed.messages[-1].role == "tool"
                assert resumed.prompt("Resume after the tool.").output == "resumed"
            finally:
                resumed.close()
        replay = resumed_provider.requests[0][0]
        assert sum(m["tool_call_id"] == "evidence-call" for m in replay) == 1
        requests.append(provider.requests + resumed_provider.requests)
    assert requests[0] == requests[1]
    assert {row["status"] for row in registry.snapshots()} == {"failed", "completed"}


def test_compaction_handoff_cache_and_continuity_projection(registry, tmp_path):
    config = RunConfig(
        root=tmp_path,
        include_agents_file=False,
        sys_prompt="Retain the pending question.",
        compaction=CompactionPolicy(
            max_total_tokens=500,
            reserved_output_tokens=50,
            keep_recent_tokens=40,
            recent_user_tokens=40,
        ),
        messages=[Message.user("older request"), Message.assistant("alpha " * 2000)],
    )
    seed = make_seed(tmp_path, config)
    requests = []
    handoffs = []
    for tracked in (False, True):
        provider = RecordingProvider(
            [
                Message.assistant("handoff from older work"),
                Message.assistant("continued"),
                Message.assistant("next answer"),
            ]
        )
        with registry.bind(session_id="owner") if tracked else nullcontext():
            agent = Agent.load(seed, provider=provider, config=config)
            try:
                assert agent.prompt("Continue.").output == "continued"
                assert agent.prompt("Next question.").output == "next answer"
                handoffs.append(
                    [
                        entry.metadata["compaction_handoff"]
                        for entry in agent.conversation_entries
                        if entry.kind == "memory_snapshot"
                    ]
                )
            finally:
                agent.close()
        assert len(provider.requests) == 3
        summary, resumed, followup = provider.requests
        assert summary[0][-1]["content"] == COMPACTION_SUMMARY_PROMPT
        assert summary[3] == "compaction_summary"
        assert "alpha alpha" not in str(resumed[0])
        assert "handoff from older work" in str(resumed[0])
        assert [call[2].response_continuity for call in provider.requests] == [
            "continue",
            "reset",
            "continue",
        ]
        assert (
            summary[2].cache_scope == resumed[2].cache_scope == followup[2].cache_scope
        )
        requests.append(provider.requests)
    assert requests[0] == requests[1]
    assert handoffs[0] == handoffs[1] and handoffs[0]
    assert len(registry.snapshots()) == 2
