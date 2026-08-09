from __future__ import annotations

# ruff: noqa: D100, D103, S101

import inspect
from typing import cast

import pytest

from yoke.agent.models import CompactionHandoff
from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.session_tree import AuditProjection
from yoke.agent.session_tree import DuplicateEntryError
from yoke.agent.session_tree import EntryRef
from yoke.agent.session_tree import ForeignEntryError
from yoke.agent.session_tree import ForwardParentError
from yoke.agent.session_tree import InvalidCurrentError
from yoke.agent.session_tree import InvalidToolSequenceError
from yoke.agent.session_tree import MissingParentError
from yoke.agent.session_tree import ParentCycleError
from yoke.agent.session_tree import ProviderProjection
from yoke.agent.session_tree import RuntimeProjection
from yoke.agent.session_tree import ScrollbackProjection
from yoke.agent.session_tree import SessionTree

_MEMORY_PREFIX = (
    "Another language model started to solve this problem and produced a "
    "summary of its work.\nUse this summary to continue the task without "
    "redoing already completed investigation.\nHere is the summary:\n"
)


def _texts(messages) -> list[str | None]:
    return [message.text_content() for message in messages]


def test_intent_appends_and_checkout_create_a_branch() -> None:
    tree = SessionTree.from_messages([Message.user("root")])
    root = tree.current
    first = tree.append_message(Message.assistant("first"))
    assert root is not None

    tree.checkout(root)
    second = tree.append_message(Message.assistant("second"))
    audit = tree.project(AuditProjection())

    assert first != second
    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "root",
        "second",
    ]
    assert len(audit.items) == 3
    assert sum(item.current for item in audit.items) == 1
    assert [item.child_count for item in audit.items if item.depth == 0] == [2]


def test_navigation_preview_owns_before_user_and_abandoned_branch_rules() -> None:
    tree = SessionTree.from_messages(
        [
            Message.user("root"),
            Message.assistant("answer"),
            Message.user("edit this"),
            Message.assistant("abandoned"),
        ]
    )
    target = next(
        item.ref
        for item in tree.project(AuditProjection()).items
        if item.message is not None and item.message.text_content() == "edit this"
    )

    preview = tree.preview_navigation(target)
    outcome = tree.navigate(target, branch_summary="Keep the decision.")

    assert preview.editor_text == "edit this"
    assert [
        item.message.text_content() if item.message is not None else None
        for item in preview.abandoned
    ] == ["abandoned"]
    assert outcome.editor_text == "edit this"
    assert outcome.summary_appended
    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "root",
        "answer",
        "Branch summary from the path you left:\n\nKeep the decision.",
    ]


def test_label_and_interrupted_turn_intents_do_not_expose_parents() -> None:
    tree = SessionTree.from_messages([Message.user("root")])
    current = tree.current
    assert current is not None

    tree.set_label(current, "  release   point ")
    appended = tree.append_interrupted_turn(
        user_message=Message.user("active request"),
        notice="Interrupted.",
    )

    audit = tree.project(AuditProjection())
    assert audit.items[0].label == "release point"
    assert len(appended) == 2
    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "root",
        "active request",
        "Interrupted.",
    ]


def test_system_control_and_checkpoint_appends_assign_topology() -> None:
    tree = SessionTree.from_messages([Message.user("large history")])
    tree.append_system_event(
        Message.system("activated skill"), metadata={"skill": "review"}
    )
    tree.append_control({"event": "turn-started"})
    checkpoint = tree.append_checkpoint(
        "compact state",
        retained_messages=[Message.user("keep")],
        mid_turn=True,
    )
    tree.append_message(Message.assistant("continue"))

    runtime = tree.project(RuntimeProjection())
    provider = tree.project(ProviderProjection())
    scrollback = tree.project(ScrollbackProjection())

    assert runtime.checkpoint is not None
    assert runtime.checkpoint.ref == checkpoint
    assert runtime.checkpoint.mid_turn
    runtime_text = _texts(runtime.messages)
    assert runtime_text[0:2] == ["activated skill", "keep"]
    assert "compact state" in (runtime_text[2] or "")
    assert runtime_text[3] == "continue"
    assert _texts(provider.messages) == runtime_text
    assert _texts(scrollback.messages) == ["large history", "continue"]


def test_legacy_checkpoint_without_retained_payload_keeps_prior_users() -> None:
    tree = SessionTree.from_messages(
        [
            Message.user("first intent"),
            Message.assistant("old work"),
            Message.user("latest intent"),
        ]
    )
    tree.append_checkpoint("legacy handoff")
    exported = tree.export()
    snapshot_entry = next(
        entry for entry in exported.entries if entry.kind == "memory_snapshot"
    )
    handoff = cast(dict[str, object], snapshot_entry.metadata["compaction_handoff"])
    handoff.pop("retained_messages")
    handoff["retained_user_messages"] = 2

    restored = SessionTree.restore(
        exported.entries,
        leaf_id=exported.leaf_id,
    )

    assert _texts(restored.project(ProviderProjection()).messages) == [
        "first intent",
        "latest intent",
        "legacy handoff",
    ]


def test_append_active_skills_assigns_topology_and_metadata() -> None:
    tree = SessionTree.from_messages([Message.user("root")])
    skill = ActiveSkill(
        name="review",
        activation_id="activation-1",
        description="Review changes",
        source_path="<inline>",
        content="# Review",
    )

    appended = tree.append_active_skills([skill])

    assert len(appended) == 1
    exported = tree.export_for_persistence()
    event = exported.entries[-1]
    assert event.kind == "skill_event"
    assert event.metadata == {
        "skill_name": "review",
        "skill_activation_id": "activation-1",
    }
    assert event.message is not None
    assert "# Review" in (event.message.text_content() or "")


def test_stale_bypass_checkpoint_does_not_leak_to_active_branch() -> None:
    tree = SessionTree.from_messages([Message.user("covered")])
    covered = tree.current
    tree.append_checkpoint("summary")
    assert covered is not None
    tree.checkout(covered)
    tree.append_message(Message.user("tail"))

    runtime = tree.project(RuntimeProjection())

    assert runtime.checkpoint is None
    assert _texts(runtime.messages) == ["covered", "tail"]
    assert _texts(tree.project(ProviderProjection()).messages) == [
        "covered",
        "tail",
    ]
    assert _texts(tree.project(ScrollbackProjection()).messages) == [
        "covered",
        "tail",
    ]


def test_detached_legacy_handoff_scrollback_reconnects_then_limits() -> None:
    historical_user = ConversationEntry(
        id="history-user", kind="user", message=Message.user("inspect")
    )
    historical_agent = ConversationEntry(
        id="history-agent",
        kind="assistant",
        message=Message.assistant("found"),
        parent_id=historical_user.id,
    )
    summary = ConversationEntry(
        id="summary", kind="compaction_summary", parent_id=historical_agent.id
    )
    snapshot = ConversationEntry(
        id="snapshot",
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="detached state"
        ).model_dump(),
    )
    handoff = ConversationEntry(
        id="handoff",
        kind="user",
        message=Message.user(f"{_MEMORY_PREFIX}detached state"),
    )
    continuation = ConversationEntry(
        id="continuation",
        kind="user",
        message=Message.user("continue"),
        parent_id=handoff.id,
    )
    tree = SessionTree.restore(
        [
            historical_user,
            historical_agent,
            summary,
            snapshot,
            handoff,
            continuation,
        ],
        leaf_id=continuation.id,
    )

    scrollback = tree.project(ScrollbackProjection(limit=2))

    assert _texts(scrollback.messages) == ["found", "continue"]
    assert scrollback.omitted_count == 1
    assert scrollback.notice is not None
    assert _texts(tree.project(RuntimeProjection()).messages) == [
        f"{_MEMORY_PREFIX}detached state",
        "continue",
    ]


def test_nested_legacy_handoffs_remove_retained_message_copies() -> None:
    historical_user = ConversationEntry(kind="user", message=Message.user("inspect"))
    historical_agent = ConversationEntry(
        kind="assistant",
        message=Message.assistant("found"),
        parent_id=historical_user.id,
    )
    first_summary = ConversationEntry(
        kind="compaction_summary", parent_id=historical_agent.id
    )
    first_snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=first_summary.id,
        metadata=MemorySnapshot(
            id="memory-1",
            summary_text="first state",
            compaction_handoff=CompactionHandoff(
                summary_text="first state",
                reason="forced",
                boundary="user",
                summarized_messages=2,
                retained_user_messages=1,
                retained_messages=[cast(Message, historical_user.message)],
                generation=1,
            ),
        ).model_dump(),
    )
    first_handoff = ConversationEntry(
        kind="user",
        message=Message.user(f"{_MEMORY_PREFIX}first state"),
        parent_id=first_snapshot.id,
    )
    repeated_historical_user = ConversationEntry(
        kind="user",
        message=historical_user.message,
        parent_id=first_handoff.id,
    )
    continuation = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=repeated_historical_user.id,
    )
    continuation_agent = ConversationEntry(
        kind="assistant",
        message=Message.assistant("working"),
        parent_id=continuation.id,
    )
    second_summary = ConversationEntry(
        kind="compaction_summary", parent_id=continuation_agent.id
    )
    second_snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=second_summary.id,
        metadata=MemorySnapshot(
            id="memory-2",
            summary_text="second state",
            compaction_handoff=CompactionHandoff(
                summary_text="second state",
                reason="forced",
                boundary="user",
                summarized_messages=4,
                retained_user_messages=2,
                retained_messages=[
                    cast(Message, historical_user.message),
                    cast(Message, continuation.message),
                ],
                generation=2,
            ),
        ).model_dump(),
    )
    detached_handoff = ConversationEntry(
        kind="user",
        message=Message.user(f"{_MEMORY_PREFIX}second state"),
    )
    repeated_user = ConversationEntry(
        kind="user",
        message=historical_user.message,
        parent_id=detached_handoff.id,
    )
    repeated_continuation = ConversationEntry(
        kind="user",
        message=continuation.message,
        parent_id=repeated_user.id,
    )
    final_agent = ConversationEntry(
        kind="assistant",
        message=Message.assistant("done"),
        parent_id=repeated_continuation.id,
    )
    tree = SessionTree.restore(
        [
            historical_user,
            historical_agent,
            first_summary,
            first_snapshot,
            first_handoff,
            repeated_historical_user,
            continuation,
            continuation_agent,
            second_summary,
            second_snapshot,
            detached_handoff,
            repeated_user,
            repeated_continuation,
            final_agent,
        ],
        leaf_id=final_agent.id,
    )

    scrollback = tree.project(ScrollbackProjection())

    assert _texts(scrollback.messages) == [
        "inspect",
        "found",
        "continue",
        "working",
        "done",
    ]


def test_detached_handoff_uses_latest_matching_legacy_checkpoint() -> None:
    old_user = ConversationEntry(kind="user", message=Message.user("old"))
    old_snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=old_user.id,
        metadata=MemorySnapshot(id="old", summary_text="repeated summary").model_dump(),
    )
    recent_user = ConversationEntry(kind="user", message=Message.user("recent"))
    recent_snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=recent_user.id,
        metadata=MemorySnapshot(
            id="recent", summary_text="repeated summary"
        ).model_dump(),
    )
    handoff = ConversationEntry(
        kind="user",
        message=Message.user(f"{_MEMORY_PREFIX}repeated summary"),
    )
    continuation = ConversationEntry(
        kind="user",
        parent_id=handoff.id,
        message=Message.user("continue"),
    )
    tree = SessionTree.restore(
        [
            old_user,
            old_snapshot,
            recent_user,
            recent_snapshot,
            handoff,
            continuation,
        ],
        continuation.id,
    )

    scrollback = tree.project(ScrollbackProjection())

    assert _texts(scrollback.messages) == ["recent", "continue"]


def test_detached_handoff_never_uses_a_future_matching_snapshot() -> None:
    prior_user = ConversationEntry(
        id="prior-user", kind="user", message=Message.user("prior history")
    )
    prior_snapshot = ConversationEntry(
        id="prior-snapshot",
        kind="memory_snapshot",
        parent_id=prior_user.id,
        metadata=MemorySnapshot(id="prior", summary_text="future state").model_dump(),
    )
    handoff = ConversationEntry(
        id="handoff",
        kind="user",
        message=Message.user(f"{_MEMORY_PREFIX}future state"),
    )
    continuation = ConversationEntry(
        id="continuation",
        kind="user",
        parent_id=handoff.id,
        message=Message.user("continue"),
    )
    future_user = ConversationEntry(
        id="future-user", kind="user", message=Message.user("future history")
    )
    future_snapshot = ConversationEntry(
        id="future-snapshot",
        kind="memory_snapshot",
        parent_id=future_user.id,
        metadata=MemorySnapshot(id="future", summary_text="future state").model_dump(),
    )
    tree = SessionTree.restore(
        [
            prior_user,
            prior_snapshot,
            handoff,
            continuation,
            future_user,
            future_snapshot,
        ],
        continuation.id,
    )

    assert _texts(tree.project(ScrollbackProjection()).messages) == [
        "prior history",
        "continue",
    ]


def test_reconcile_merges_branch_and_selects_it() -> None:
    tree = SessionTree.from_messages(
        [Message.user("root"), Message.assistant("original")]
    )
    branch = SessionTree.restore(tree.entries, leaf_id=tree.leaf_id)
    root = next(
        item.ref for item in branch.project(AuditProjection()).items if item.depth == 0
    )
    branch.checkout(root)
    branch.append_message(Message.assistant("alternative"))

    tree.reconcile(branch.entries, leaf_id=branch.leaf_id)

    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "root",
        "alternative",
    ]
    audit = tree.project(AuditProjection())
    assert {
        item.message.text_content() for item in audit.items if item.message is not None
    } == {"alternative", "original", "root"}


def test_authoritative_reconcile_does_not_match_by_message_equality() -> None:
    tree = SessionTree.from_messages([Message.user("same")])
    independent = SessionTree.from_messages(
        [Message.user("same"), Message.assistant("new lineage")]
    )

    tree.reconcile(
        independent.export_for_persistence().entries,
        leaf_id=independent.leaf_id,
    )

    audit = tree.project(AuditProjection())
    assert sum(item.depth == 0 for item in audit.items) == 2
    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "same",
        "new lineage",
    ]


def test_explicit_legacy_reconcile_can_match_equal_message_prefix() -> None:
    tree = SessionTree.from_messages([Message.user("same")])
    imported = SessionTree.from_messages(
        [Message.user("same"), Message.assistant("legacy continuation")]
    )

    tree.reconcile_legacy_import(imported.entries, leaf_id=imported.leaf_id)

    audit = tree.project(AuditProjection())
    assert sum(item.depth == 0 for item in audit.items) == 1


def test_restore_validates_invalid_topology() -> None:
    duplicate = ConversationEntry(id="same", kind="user")
    with pytest.raises(DuplicateEntryError, match="duplicate"):
        SessionTree.restore([duplicate, duplicate.model_copy(deep=True)])

    missing = ConversationEntry(id="child", kind="user", parent_id="not-present")
    with pytest.raises(MissingParentError, match="missing parent"):
        SessionTree.restore([missing])

    forward = ConversationEntry(id="first", kind="user", parent_id="second")
    second = ConversationEntry(id="second", kind="assistant")
    with pytest.raises(ForwardParentError, match="later in event order"):
        SessionTree.restore([forward, second])
    repaired = SessionTree.import_legacy([forward, second])
    assert len(repaired.export_for_persistence().entries) == 2

    cycle = ConversationEntry(id="cycle", kind="user", parent_id="cycle")
    with pytest.raises(ParentCycleError, match="cycle"):
        SessionTree.restore([cycle])

    root = ConversationEntry(id="root", kind="user")
    with pytest.raises(InvalidCurrentError, match="leaf"):
        SessionTree.restore([root], leaf_id="unknown")


def test_assume_linear_is_explicit_and_references_are_session_scoped() -> None:
    first = ConversationEntry(id="first", kind="user", message=Message.user("first"))
    second = ConversationEntry(
        id="second", kind="assistant", message=Message.assistant("second")
    )
    tree = SessionTree.restore([first, second], assume_linear=True)
    other = SessionTree.from_messages([Message.user("other")])
    foreign = other.current

    assert _texts(tree.project(RuntimeProjection()).messages) == [
        "first",
        "second",
    ]
    assert foreign is not None
    with pytest.raises(ForeignEntryError, match="different session"):
        tree.checkout(foreign)


def test_exports_and_views_are_defensive_copies() -> None:
    source = Message.user("original")
    tree = SessionTree.from_messages([source])
    source.content = "changed source"
    exported = tree.export()
    exported_message = exported.entries[0].message
    assert exported_message is not None
    exported_message.content = "changed export"
    view_message = tree.project(RuntimeProjection()).messages[0].to_message()
    view_message.content = "changed view"

    assert _texts(tree.project(RuntimeProjection()).messages) == ["original"]
    assert tree.leaf_id == exported.leaf_id
    assert isinstance(exported.entries, tuple)


def test_append_message_rejects_invalid_role_tool_field_combinations() -> None:
    call = ToolCall(
        id="call-1",
        function=ToolFunction(name="inspect", arguments="{}"),
    )
    malformed = [
        Message(role="user", content="bad", tool_calls=[call]),
        Message(role="user", content="bad", tool_call_id="call-1"),
        Message(role="assistant", content="bad", tool_call_id="call-1"),
        Message(
            role="tool",
            content="bad",
            tool_call_id="call-1",
            tool_calls=[call],
        ),
    ]

    for message in malformed:
        tree = SessionTree.from_messages([])
        with pytest.raises(InvalidToolSequenceError):
            tree.append_message(message)

    imported = SessionTree.from_messages(malformed)
    assert len(imported.export_for_persistence().entries) == len(malformed)


def test_persisted_id_adapter_is_ingress_only_and_refs_are_required() -> None:
    tree = SessionTree.from_messages([Message.user("root")])
    exported = tree.export_for_persistence()
    target = tree.ref_from_persisted_id(exported.entries[0].id)

    assert tree.preview_navigation(target).current
    raw_target = cast(EntryRef, exported.entries[0].id)
    with pytest.raises(ForeignEntryError):
        tree.checkout(raw_target)


def test_filtered_import_reconnects_descendants_of_dropped_messages() -> None:
    call = ToolCall(
        id="call-1",
        function=ToolFunction(name="run", arguments="{}"),
    )
    root_message = Message.user("root")
    tail_message = Message.user("tail")
    root = ConversationEntry(kind="user", message=root_message)
    dropped = ConversationEntry(
        kind="assistant",
        message=Message(role="assistant", content="", tool_calls=[call]),
        parent_id=root.id,
    )
    tail = ConversationEntry(
        kind="user",
        message=tail_message,
        parent_id=dropped.id,
    )

    tree = SessionTree.import_filtered_transcript(
        [root, dropped, tail],
        [root_message, tail_message],
    )
    exported = tree.export_for_persistence()

    assert [entry.id for entry in exported.entries] == [root.id, tail.id]
    assert exported.entries[-1].parent_id == root.id
    assert exported.leaf_id == tail.id


def test_public_intents_do_not_accept_raw_parent_assignment() -> None:
    intent_names = (
        "append_message",
        "append_active_skills",
        "append_interrupted_turn",
        "navigate",
        "set_label",
    )

    for name in intent_names:
        assert (
            "parent_id" not in inspect.signature(getattr(SessionTree, name)).parameters
        )
