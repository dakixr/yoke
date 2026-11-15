"""Frozen requests from RuntimeAgent at 5268d70, before prompt-path refactoring."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from yoke.agent.compaction import CompactionPolicy, force_compact_agent
from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import (
    Message,
    MessageImageURL,
    MessageImageURLContentPart,
    MessageTextContentPart,
    ToolCall,
    ToolFunction,
)
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill, SkillSpec
from yoke.agent.tools import LocalTool
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex.cache import PromptCacheConfig, build_prompt_cache_key
from yoke.ai.providers.usage_context import current_usage_metric_context

FIXTURE = Path(__file__).with_name("fixtures") / "runtime_requests_5268d70.json"
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


class EchoTool(LocalTool):
    """An in-process tool whose result is independent of the environment."""

    name = "oracle_echo"
    description = "Echo a value for the request oracle."
    execute_in_process = True
    value: str

    def execute(self) -> dict[str, object]:
        """Return the supplied value."""
        return {"ok": True, "value": self.value}


def _calls(*values: str) -> Message:
    return Message(
        role="assistant",
        content="checking",
        tool_calls=[
            ToolCall(
                id=f"oracle-{value}",
                function=ToolFunction(
                    name="oracle_echo", arguments=json.dumps({"value": value})
                ),
            )
            for value in values
        ],
    )


class RecordingProvider:
    """Record the exact provider boundary and return scripted completions."""

    max_images_per_message = 50
    provider_name = "oracle"

    def __init__(
        self, responses: list[Message | ProviderError], *, images: bool = True
    ) -> None:
        self.responses = iter(responses)
        self.supports_image_inputs = images
        self.records: list[dict[str, object]] = []
        self.config = SimpleNamespace(
            model="oracle", reasoning_effort=None, prompt_cache_key="oracle-session"
        )

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Record JSON before returning or raising the next scripted outcome."""
        self.records.append(
            {
                "messages_json": json.dumps(
                    [message.model_dump(mode="json") for message in messages],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "tools": tools,
                "call_kind": current_usage_metric_context().call_kind,
                "prompt_cache_key": build_prompt_cache_key(
                    cast(PromptCacheConfig, self.config)
                ),
            }
        )
        response = next(self.responses)
        if isinstance(response, ProviderError):
            raise response
        return response.model_copy(deep=True)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Use the same cancellable route as the built-in providers."""
        assert not cancel_requested()
        return self.complete(messages, tools)


def _runtime(provider: RecordingProvider, **kwargs) -> RuntimeAgent:
    return RuntimeAgent(provider=provider, tools=[EchoTool.bind()], **kwargs)


def record_scenario(name: str) -> list[dict[str, object]]:
    """Drive tools, skills, compaction, cancellation and branches through the loop."""
    if name in {"tools-skills-images", "tools-skills-text"}:
        provider = RecordingProvider(
            [_calls("single"), _calls("left", "right"), Message.assistant("done")],
            images=name.endswith("images"),
        )
        agent = _runtime(
            provider,
            context_manager=ContextManager(instructions=[Message.system("base")]),
            available_skills=[
                SkillSpec(
                    name="oracle-skill",
                    description="An available oracle skill.",
                    root=Path("/oracle"),
                    skill_md_path=Path("/oracle/SKILL.md"),
                )
            ],
            active_skills=[
                ActiveSkill(
                    name="inline",
                    description="An active inline skill.",
                    source_path="<inline>",
                    content="Preserve these exact instructions.",
                )
            ],
        )
        try:
            result = agent.run(
                "inspect",
                user_message=Message.user(
                    [
                        MessageTextContentPart(text="inspect"),
                        MessageImageURLContentPart(
                            image_url=MessageImageURL(url=IMAGE), label="[Image #1]"
                        ),
                    ]
                ),
            )
            assert result.output == "done"
            assert result.iterations == 3
        finally:
            agent.close()
        return provider.records

    history = [
        Message.user("old request"),
        Message.assistant("old answer " + "alpha " * 400),
        Message.user("recent request"),
    ]
    if name in {"manual-compaction", "automatic-compaction", "overflow-retry"}:
        responses: list[Message | ProviderError] = [
            Message.assistant("handoff summary"),
            Message.assistant("resumed"),
        ]
        if name == "overflow-retry":
            responses.insert(
                0, ProviderError("context_length_exceeded", status_code=400)
            )
        provider = RecordingProvider(responses)
        policy = (
            CompactionPolicy(
                max_total_tokens=600,
                reserved_output_tokens=100,
                recent_user_tokens=40,
            )
            if name == "automatic-compaction"
            else CompactionPolicy()
        )
        agent = _runtime(
            provider,
            messages=history,
            context_manager=ContextManager(compaction_policy=policy),
        )
        try:
            if name == "manual-compaction":
                assert force_compact_agent(agent, agent.messages) is not None
            assert agent.run("follow up").output == "resumed"
        finally:
            agent.close()
        assert [record["call_kind"] for record in provider.records] == (
            ["model_iteration", "compaction_summary", "overflow_retry"]
            if name == "overflow-retry"
            else ["compaction_summary", "model_iteration"]
        )
        return provider.records

    if name == "stop-continue":
        provider = RecordingProvider(
            [_calls("cancelled"), Message.assistant("continued")]
        )
        agent = _runtime(provider)
        stopped = False

        def on_event(event: str, payload: dict[str, object]) -> None:
            nonlocal stopped
            if event == "model_end":
                stopped = True

        try:
            result = agent.run(
                "start", on_event=on_event, stop_requested=lambda: stopped
            )
            assert result.status == "stopped"
            assert agent.run("", append_user_message=False).output == "continued"
        finally:
            agent.close()
        return provider.records

    assert name == "tree-navigation"
    tree = SessionTree.from_messages([Message.user("root")])
    root = tree.current
    assert root is not None
    tree.append_system_event(Message.system("abandoned skill"))
    tree.append_message(Message.assistant("abandoned answer"))
    tree.checkout(root)
    tree.append_system_event(Message.system("active branch skill"))
    tree.append_message(Message.assistant("active answer"))
    tree.append_checkpoint("prior checkpoint", retained_messages=[Message.user("root")])
    provider = RecordingProvider([Message.assistant("branch answer")])
    agent = _runtime(
        provider, conversation_entries=tree.export_for_persistence().entries
    )
    try:
        assert agent.run("continue active branch").output == "branch answer"
    finally:
        agent.close()
    return provider.records


SCENARIOS = (
    "tools-skills-images",
    "tools-skills-text",
    "manual-compaction",
    "automatic-compaction",
    "overflow-retry",
    "stop-continue",
    "tree-navigation",
)


@pytest.mark.parametrize("name", SCENARIOS)
def test_runtime_provider_requests_match_baseline(name: str) -> None:
    """All serialized messages, tool schemas, call kinds and cache keys stay fixed."""
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert record_scenario(name) == expected[name]
