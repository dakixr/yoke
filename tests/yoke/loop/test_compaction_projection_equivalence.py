from __future__ import annotations

# ruff: noqa: D100,D103,S101

from collections.abc import Callable

import pytest

from yoke.agent.context import ContextManager
from yoke.agent.context.compaction_projection import (
    next_compaction_generation_from_active_path,
)
from yoke.agent.context.helpers import next_compaction_generation
from yoke.agent.context.helpers import recent_log_messages
from yoke.agent.loop.cache_scope import conversation_cache_scope
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import SessionTree

Scenario = Callable[[], list[ConversationEntry]]


def _tool_and_image_history() -> list[ConversationEntry]:
    call = ToolCall(
        id="call-1",
        function=ToolFunction(name="fd", arguments='{"pattern":"README"}'),
    )
    tree = SessionTree.from_messages(
        [
            Message.user(
                [
                    MessageTextContentPart(text="inspect this"),
                    MessageLocalImageContentPart(
                        path="/tmp/yoke-equivalence.png",
                        label="[Image #1]",
                    ),
                ]
            ),
            Message(role="assistant", content="checking", tool_calls=[call]),
        ]
    )
    tree.append_message(
        Message.tool(
            "call-1",
            '{"ok":true,"command":["fd","--print0"],"output":["README.md"],"exit_code":0}',
        ),
        metadata={"provider_result_projection": "fd"},
    )
    tree.append_message(Message.assistant("checked"))
    tree.append_message(Message.user("latest request"))
    return list(tree.export_for_persistence().entries)


def _skill_and_checkpoint_history() -> list[ConversationEntry]:
    tree = SessionTree.from_messages(
        [
            Message.user("old request"),
            Message.assistant("old answer"),
            Message.user("recent request"),
        ]
    )
    tree.append_system_event(Message.system("active skill instructions"))
    tree.append_checkpoint(
        "prior compacted state",
        retained_messages=[Message.user("recent request")],
    )
    tree.append_message(Message.assistant("continued answer"))
    call = ToolCall(
        id="call-checkpoint",
        function=ToolFunction(name="fd", arguments='{"pattern":"src"}'),
    )
    tree.append_message(Message(role="assistant", tool_calls=[call]))
    tree.append_message(
        Message.tool(
            call.id,
            '{"ok":true,"command":["fd","--print0"],"output":["src/yoke"],"exit_code":0}',
        ),
        metadata={"provider_result_projection": "fd"},
    )
    tree.append_message(Message.user("latest request"))
    return list(tree.export_for_persistence().entries)


def _branched_history_with_stale_skill() -> list[ConversationEntry]:
    tree = SessionTree.from_messages([Message.user("root request")])
    root = tree.current
    assert root is not None
    tree.append_system_event(Message.system("abandoned branch skill"))
    abandoned_call = ToolCall(
        id="call-branch",
        function=ToolFunction(name="fd", arguments='{"pattern":"old"}'),
    )
    tree.append_message(Message(role="assistant", tool_calls=[abandoned_call]))
    tree.append_message(
        Message.tool(
            abandoned_call.id,
            '{"ok":true,"command":["fd","--print0"],"output":["old.txt"],"exit_code":0}',
        )
    )
    tree.append_message(Message.assistant("abandoned answer"))
    tree.checkout(root)
    tree.append_system_event(Message.system("active branch skill"))
    active_call = ToolCall(
        id="call-branch",
        function=ToolFunction(name="fd", arguments='{"pattern":"active"}'),
    )
    tree.append_message(Message(role="assistant", tool_calls=[active_call]))
    tree.append_message(
        Message.tool(
            active_call.id,
            '{"ok":true,"command":["fd","--print0"],"output":["active.txt"],"exit_code":0}',
        ),
        metadata={"provider_result_projection": "fd"},
    )
    tree.append_message(Message.assistant("active answer"))
    tree.append_message(Message.user("active follow-up"))
    return list(tree.export_for_persistence().entries)


SCENARIOS: tuple[Scenario, ...] = (
    _tool_and_image_history,
    _skill_and_checkpoint_history,
    _branched_history_with_stale_skill,
)


def _context(entries: list[ConversationEntry]) -> tuple[ContextManager, AgentContext]:
    manager = ContextManager(instructions=[Message.system("base instructions")])
    context = manager.initialize(
        "",
        append_prompt=False,
        conversation_entries=entries,
    )
    return manager, context


def _dump(messages: list[Message]) -> list[dict[str, object]]:
    return [message.model_dump() for message in messages]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fast_generation_lookup_matches_established_projection(
    scenario: Scenario,
) -> None:
    _, context = _context(scenario())

    assert next_compaction_generation_from_active_path(
        context
    ) == next_compaction_generation(context)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_provider_projection_is_active_in_equivalence_scenarios(
    scenario: Scenario,
) -> None:
    manager, context = _context(scenario())
    provider_messages = manager.messages_for_provider(context)
    tool_contents = [
        message.plain_text_content
        for message in provider_messages
        if message.role == "tool"
    ]

    assert any(
        content is not None
        and content.startswith("ok: true\npaths[1]:")
        and '"command"' not in content
        for content in tool_contents
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fast_recent_user_selection_matches_established_projection(
    scenario: Scenario,
) -> None:
    manager, context = _context(scenario())
    expected = manager.compactor.collect_recent_user_messages(
        recent_log_messages(context),
        token_budget=manager.compaction_policy.recent_user_tokens,
    )

    established_provider_messages = manager.messages_for_provider(context)
    preparation = manager.prepare_compaction(context, reason="forced")

    assert preparation is not None
    assert _dump(preparation.messages_to_summarize) == _dump(
        established_provider_messages
    )
    assert _dump(preparation.recent_user_messages) == _dump(expected)
    assert _dump(preparation.kept_messages) == _dump(expected)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fast_post_compaction_state_matches_established_projection(
    scenario: Scenario,
) -> None:
    manager, context = _context(scenario())
    scope_before = conversation_cache_scope(context)
    preparation = manager.prepare_compaction(context, reason="forced")
    assert preparation is not None

    result = manager.apply_compaction(
        context,
        preparation,
        instruction_message=Message.user("checkpoint instruction"),
        summary_message=Message.assistant("new compacted state"),
    )
    established = manager.transcript_messages(context)

    assert _dump(context.messages) == _dump(established)
    assert _dump(result.messages) == _dump(established)
    assert _dump(manager.messages_for_provider(context)) == _dump(
        manager.messages_for_provider(
            context.model_copy(update={"messages": established}, deep=True)
        )
    )
    assert conversation_cache_scope(context) == scope_before
