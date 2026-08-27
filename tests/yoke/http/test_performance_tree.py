from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path


from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.store import SessionStore
from yoke.http.models.session import SessionForkRequest
from yoke.http.models.session import TreeNavigateRequest
from yoke.http.services.session_service import SessionService
from tests.yoke.http.performance_helpers import messages as _messages


def test_tree_interactions_use_topology_index_without_loading_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    root = ConversationEntry(id="root", kind="user", message=Message.user("root"))
    shared = ConversationEntry(
        id="shared",
        kind="assistant",
        message=Message.assistant("shared"),
        parent_id=root.id,
    )
    target_user = ConversationEntry(
        id="target-user",
        kind="user",
        message=Message.user("edit me"),
        parent_id=shared.id,
    )
    target_assistant = ConversationEntry(
        id="target-assistant",
        kind="assistant",
        message=Message.assistant("target branch"),
        parent_id=target_user.id,
    )
    current_user = ConversationEntry(
        id="current-user",
        kind="user",
        message=Message.user("current branch"),
        parent_id=shared.id,
    )
    current_assistant = ConversationEntry(
        id="current-assistant",
        kind="assistant",
        message=Message.assistant("current tail"),
        parent_id=current_user.id,
    )
    record = SessionRecord(
        id="branch",
        root=str(tmp_path),
        created_at="2026-08-27T00:00:00+00:00",
        updated_at="2026-08-27T00:00:01+00:00",
        leaf_id=current_assistant.id,
        conversation_entries=[
            root,
            shared,
            target_user,
            target_assistant,
            current_user,
            current_assistant,
        ],
    )
    store._write_session_record(record)
    store._update_index(record)
    service = SessionService(store)
    initial_tree = service.tree("branch", limit=20, cursor=None)
    original_load = store.load

    def fail(*_args, **_kwargs):
        raise AssertionError("tree interactions must not deserialize session history")

    monkeypatch.setattr(store, "load", fail)
    preview = service.navigation_preview(
        "branch",
        target_id=target_user.id,
        include_abandoned=True,
    )
    assert preview.data.editor_text == "edit me"
    assert preview.data.abandoned_total == 2
    assert [item.id for item in preview.data.abandoned] == [
        current_user.id,
        current_assistant.id,
    ]

    labeled = service.set_tree_label(
        "branch",
        target_user.id,
        expected_revision=initial_tree.data.revision,
        label="  chosen   branch  ",
    )
    assert labeled.data.entry.label == "chosen branch"

    navigated = service.navigate_tree(
        "branch",
        TreeNavigateRequest(
            expected_revision=labeled.data.revision,
            target_id=target_user.id,
            branch_summary="  preserve this handoff  ",
        ),
    )
    assert navigated.data.editor_text == "edit me"
    assert navigated.data.summary_added is True
    assert navigated.data.leaf_id not in {None, shared.id, target_user.id}
    index_entry = store.index_entry("branch")
    assert index_entry is not None
    assert index_entry.entry_count == 7

    persisted = original_load("branch")
    assert persisted.leaf_id == navigated.data.leaf_id
    by_id = {entry.id: entry for entry in persisted.conversation_entries}
    assert by_id[target_user.id].metadata["label"] == "chosen branch"
    navigated_leaf = navigated.data.leaf_id
    assert navigated_leaf is not None
    summary = by_id[navigated_leaf]
    assert summary.kind == "branch_summary"
    assert summary.parent_id == shared.id
    assert summary.metadata["summary"] == "preserve this handoff"


def test_fork_clones_canonical_session_without_loading_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    source = store.save("large", _messages(600, payload=2048), root=tmp_path)
    service = SessionService(store)
    assert service.message_index._ensure("large") is not None
    target_id = source.conversation_entries[200].id
    original_load = store.load

    def fail(*_args, **_kwargs):
        raise AssertionError("canonical forks must not deserialize session history")

    monkeypatch.setattr(store, "load", fail)
    full = service.fork_session(
        "large",
        SessionForkRequest(id="full-fork", title="Full fork"),
    )
    branch = service.fork_session(
        "large",
        SessionForkRequest(
            id="branch-fork",
            title="Branch fork",
            from_entry_id=target_id,
        ),
    )

    assert full.id == "full-fork"
    assert full.title == "Full fork"
    assert full.tree.entry_count == len(source.conversation_entries)
    assert full.tree.leaf_id == source.leaf_id
    assert branch.id == "branch-fork"
    assert branch.title == "Branch fork"
    assert branch.tree.entry_count == len(source.conversation_entries)
    assert branch.tree.leaf_id == target_id

    full_record = original_load("full-fork")
    branch_record = original_load("branch-fork")
    assert full_record.id == "full-fork"
    assert full_record.leaf_id == source.leaf_id
    assert len(full_record.conversation_entries) == len(source.conversation_entries)
    assert branch_record.id == "branch-fork"
    assert branch_record.leaf_id == target_id
    assert len(branch_record.conversation_entries) == len(source.conversation_entries)

    original_scan = service.message_index._scan

    def guard_scan(*args, **kwargs):
        if kwargs.get("start") == 0:
            raise AssertionError("forked sidecar should avoid a full topology rescan")
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(service.message_index, "_scan", guard_scan)
    page = service.messages("full-fork", limit=10, order="desc", cursor=None)
    assert len(page.data) == 10
