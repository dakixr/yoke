"""Approved interruption and retention deltas at the provider boundary."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from tests.yoke.loop.request_state_support import conversation_cache_scope
from tests.yoke.loop.test_compaction_projection_equivalence import SCENARIOS
from tests.yoke.loop.test_runtime_request_differential import RecordingProvider
from yoke.agent.context import ContextManager
from yoke.agent.context.helpers import append_control_message
from yoke.agent.interruptions import (
    INTERRUPTED_RESPONSE_NOTE,
    INTERRUPTED_TURN_NOTICE,
    is_interruption_notice,
)
from yoke.agent.loop import AgentStoppedError, RuntimeAgent
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.session_tree import SessionTree
from yoke.cli.backend.headless import projected_message
from yoke.cli.render.scrollback import print_session_scrollback
from yoke.http.services.projectors import project_entry
from yoke.session import SessionStore
from yoke.session.interrupt import interrupted_turn_snapshot


def _dump(messages):
    return [message.model_dump_json() for message in messages]


def test_five_fast_messages_collapse_each_interruption_run():
    class InterruptedProvider(RecordingProvider):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if len(self.records) <= 5:
                raise AgentStoppedError("interrupted before output")
            return response

    provider = InterruptedProvider([Message.assistant("answer")] * 7)
    agent = RuntimeAgent(provider=provider, tools=[])
    try:
        for prompt in ["start", *[f"quick {n}" for n in range(1, 6)]]:
            try:
                agent.run(prompt)
            except AgentStoppedError:
                _, entries = interrupted_turn_snapshot(
                    messages=agent.messages,
                    entries=agent.conversation_entries,
                    user_message=None,
                )
                assert entries[-1].kind == "control"
                agent.load_conversation(conversation_entries=entries)
        # Actual output starts a separate interruption run.
        _, entries = interrupted_turn_snapshot(
            messages=agent.messages,
            entries=agent.conversation_entries,
            user_message=None,
        )
        agent.load_conversation(conversation_entries=entries)
        agent.run("another run")
    finally:
        agent.close()
    requests = [json.loads(str(record["messages_json"])) for record in provider.records]
    assert len(requests) == 7
    for index, request in enumerate(requests[:6]):
        assert sum(m["content"] == INTERRUPTED_RESPONSE_NOTE for m in request) == (
            0 if index == 0 else 1
        )
        assert not any(m["content"] == INTERRUPTED_TURN_NOTICE for m in request)
    assert [(m["role"], m["content"]) for m in requests[5]] == [
        ("user", "start"),
        ("system", INTERRUPTED_RESPONSE_NOTE),
        *[("user", f"quick {n}") for n in range(1, 6)],
    ]
    assert sum(m["content"] == INTERRUPTED_RESPONSE_NOTE for m in requests[-1]) == 2
    assert all(
        m["role"] == "system"
        for request in requests
        for m in request
        if m["content"] == INTERRUPTED_RESPONSE_NOTE
    )


@pytest.mark.parametrize("kind", ["assistant", "control"])
@pytest.mark.parametrize("owned", [False, True])
def test_legacy_load_projects_without_rewriting_stored_entries(tmp_path, kind, owned):
    entries = [
        ConversationEntry(id="u", kind="user", message=Message.user("start")),
        ConversationEntry(
            id="i",
            parent_id="u",
            kind=kind,
            message=Message.assistant(INTERRUPTED_TURN_NOTICE),
        ),
        ConversationEntry(
            id="v", parent_id="i", kind="user", message=Message.user("next")
        ),
    ]
    store = SessionStore(tmp_path / "sessions")
    try:
        store.save("legacy", [], conversation_entries=entries)
        before = store.load("legacy").model_dump_json()
        manager = ContextManager(instructions=[Message.system("base")])
        loaded = store.load("legacy").conversation_entries
        initialize = manager.initialize_owned if owned else manager.initialize
        context = initialize("", conversation_entries=loaded, append_prompt=False)
        assert _dump(manager.messages_for_provider(context)) == _dump(
            [
                Message.system("base"),
                Message.user("start"),
                Message.system(INTERRUPTED_RESPONSE_NOTE),
                Message.user("next"),
            ]
        )
        assert store.load("legacy").model_dump_json() == before
        assert loaded[1].kind == kind
        assert loaded[1].message == Message.assistant(INTERRUPTED_TURN_NOTICE)
    finally:
        store.database.close()


@pytest.mark.parametrize("kind", ["assistant", "control"])
def test_web_and_cli_show_only_interrupted_marker(kind):
    entry = ConversationEntry(
        kind=kind, message=Message.assistant(INTERRUPTED_TURN_NOTICE)
    )
    public = project_entry(entry).model_dump(mode="json", by_alias=True)
    assert public["type"] == "control"
    assert public["kind"] == "control"
    assert public["control"] == "interrupted"
    assert public["text"] is None
    message = projected_message(public)
    assert message is not None
    assert message == Message.system(INTERRUPTED_RESPONSE_NOTE)
    output = io.StringIO()
    print_session_scrollback(Console(file=output, color_system=None), [message])
    assert output.getvalue() == "Interrupted\n"
    # Other control events still stay out of terminal scrollback.
    assert projected_message({"type": "control", "control": "skill_event"}) is None


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_interruption_delta_preserves_tools_skills_checkpoints_images_and_branches(
    scenario,
):
    entries = scenario()
    manager = ContextManager(instructions=[Message.system("base")])
    context = manager.initialize("", conversation_entries=entries, append_prompt=False)
    baseline = manager.messages_for_provider(context)
    scope = conversation_cache_scope(context)
    # Legacy and current markers collapse together, even across internal events.
    manager.append_message(context, Message.assistant(INTERRUPTED_TURN_NOTICE))
    append_control_message(context, Message.system("internal instruction"), metadata={})
    manager.append_message(context, Message.system(INTERRUPTED_RESPONSE_NOTE))
    manager.append_message(context, Message.user("next"))
    assert _dump(manager.messages_for_provider(context)) == _dump(
        [
            *baseline,
            Message.system("internal instruction"),
            Message.system(INTERRUPTED_RESPONSE_NOTE),
            Message.user("next"),
        ]
    )
    assert conversation_cache_scope(context) == scope


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_compaction_removes_only_internal_kept_messages(scenario):
    manager = ContextManager(instructions=[Message.system("base")])
    context = manager.initialize(
        "", conversation_entries=scenario(), append_prompt=False
    )
    baseline = manager.prepare_compaction(context, reason="forced")
    assert baseline is not None
    expected_summary_input = manager.messages_for_provider(context)
    command = Message.user("Background command completion notice: finished")
    branch = Message.user(
        "Branch summary from the path you left:\n\nfinished investigation"
    )
    append_control_message(
        context, command, metadata={"internal_event": "command_completion"}
    )
    tree = SessionTree.restore(
        context.conversation_log.entries, context.conversation_log.leaf_id
    )
    target = tree.current
    assert target is not None
    tree.append_message(Message.assistant("abandoned"))
    tree.navigate(target, branch_summary="finished investigation")
    exported = tree.export_for_persistence()
    context = manager.initialize(
        "", conversation_entries=exported.entries, append_prompt=False
    )
    preparation = manager.prepare_compaction(context, reason="forced")
    assert preparation is not None
    assert _dump(preparation.messages_to_summarize) == _dump(
        [
            *expected_summary_input,
            command,
            branch,
        ]
    )
    assert _dump(preparation.kept_messages) == _dump(baseline.kept_messages)
    assert _dump(preparation.recent_user_messages) == _dump(
        baseline.recent_user_messages
    )
    # Established role-based selection would have kept both internal notices.
    old_kept = manager.compactor.collect_recent_user_messages(
        [
            ConversationEntry(kind="user", message=m)
            for m in context.messages
            if m.role == "user"
        ],
        token_budget=manager.compaction_policy.recent_user_tokens,
    )
    assert old_kept[-2:] == [command, branch]
    manager.apply_compaction(
        context,
        preparation,
        instruction_message=Message.user("summarize"),
        summary_message=Message.assistant("handoff"),
    )
    manager.append_message(context, Message.user("new request"))
    manager.append_message(context, Message.assistant("response"))
    skills = [m for m in expected_summary_input if m.role == "system"][1:]
    assert _dump(manager.messages_for_provider(context)) == _dump(
        [
            Message.system("base"),
            *skills,
            *baseline.kept_messages,
            Message.assistant("handoff"),
            Message.user("new request"),
            Message.assistant("response"),
        ]
    )
    again = manager.prepare_compaction(context, reason="forced")
    assert again is not None
    assert _dump(again.kept_messages) == _dump(
        [
            *baseline.kept_messages,
            Message.user("new request"),
        ]
    )
    assert not any(is_interruption_notice(m) for m in again.kept_messages)


def test_message_only_load_keeps_current_and_legacy_markers():
    for marker in [
        Message.assistant(INTERRUPTED_TURN_NOTICE),
        Message.system(INTERRUPTED_RESPONSE_NOTE),
    ]:
        manager = ContextManager()
        context = manager.initialize("next", messages=[marker])
        assert context.instructions == []
        assert context.conversation_log.entries[0].kind == "control"
        assert _dump(manager.messages_for_provider(context)) == _dump(
            [
                Message.system(INTERRUPTED_RESPONSE_NOTE),
                Message.user("next"),
            ]
        )


def test_retention_uses_kind_even_when_user_repeats_internal_text():
    tree = SessionTree.from_messages([Message.user("root")])
    root = tree.current
    assert root is not None
    tree.append_message(Message.user("abandoned user request"))
    tree.checkout(root)
    internal_text = "Branch summary from the path you left:\n\nsame text"
    tree.append_control({}, message=Message.user(internal_text))
    tree.append_tool_context(Message.user("tool context"))
    tree.append_message(Message.user(internal_text))
    manager = ContextManager()
    context = manager.initialize(
        "", conversation_entries=tree.export().entries, append_prompt=False
    )
    preparation = manager.prepare_compaction(context, reason="forced")
    assert preparation is not None
    assert _dump(preparation.kept_messages) == _dump(
        [
            Message.user("root"),
            Message.user(internal_text),
        ]
    )


def test_repeated_compaction_keeps_truncation_and_filters_legacy_retained_notices():
    from yoke.agent.compaction import CompactionPolicy

    manager = ContextManager(compaction_policy=CompactionPolicy(recent_user_tokens=32))
    context = manager.initialize(
        "latest", [Message.user("old request " + "alpha " * 500)]
    )
    first = manager.prepare_compaction(context, reason="forced")
    assert first is not None
    assert "truncated during context compaction" in str(first.kept_messages[0].content)
    manager.apply_compaction(
        context,
        first,
        instruction_message=Message.user("summarize"),
        summary_message=Message.assistant("handoff"),
    )
    restored = manager.initialize(
        "", conversation_entries=context.conversation_log.entries, append_prompt=False
    )
    second = manager.prepare_compaction(restored, reason="forced")
    assert second is not None
    assert _dump(second.kept_messages) == _dump(first.kept_messages)

    real = Message.user("actual prompt")
    internal = Message.user("command completed")
    tree = SessionTree.from_messages([real])
    tree.append_control({}, message=internal)
    tree.append_checkpoint("legacy handoff", retained_messages=[real, internal])
    restored = manager.initialize(
        "", conversation_entries=tree.export().entries, append_prompt=False
    )
    preparation = manager.prepare_compaction(restored, reason="forced")
    assert preparation is not None
    assert preparation.kept_messages == [real]


def test_codex_serializes_interruption_in_place_as_developer_instruction():
    from yoke.ai.providers.codex.subscription.messages import convert_messages

    manager = ContextManager(instructions=[Message.system("base instructions")])
    context = manager.initialize(
        "next",
        [
            Message.user("start"),
            Message.assistant(INTERRUPTED_TURN_NOTICE),
            Message.assistant(INTERRUPTED_TURN_NOTICE),
        ],
    )
    instructions, items = convert_messages(manager.messages_for_provider(context))
    assert instructions == "base instructions"
    assert [item["role"] for item in items] == ["user", "developer", "user"]
    assert items[1]["content"] == [
        {"type": "input_text", "text": INTERRUPTED_RESPONSE_NOTE}
    ]
