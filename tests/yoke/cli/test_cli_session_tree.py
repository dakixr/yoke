# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.agent.state import merge_conversation_branch
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import persist_session_state
from yoke.cli.runtime.session import save_agent_session_state
from yoke.cli.session import SessionStore
from yoke.agent.state import AgentState


def test_session_store_migrates_legacy_linear_session_to_tree(
    tmp_path: Path,
) -> None:
    store = SessionStore(directory=tmp_path)
    payload = {
        "version": 3,
        "id": "legacy-tree",
        "conversation_entries": [
            {
                "kind": "user",
                "message": {"role": "user", "content": "one"},
                "metadata": {},
            },
            {
                "kind": "assistant",
                "message": {"role": "assistant", "content": "two"},
                "metadata": {},
            },
        ],
    }
    (tmp_path / "legacy-tree.json").write_text(json.dumps(payload), encoding="utf-8")

    record = store.load("legacy-tree")

    assert record.version == 4
    assert record.leaf_id == record.conversation_entries[-1].id
    assert record.conversation_entries[0].parent_id is None
    assert record.conversation_entries[1].parent_id == record.conversation_entries[0].id
    assert not (tmp_path / "legacy-tree.json").exists()
    assert (tmp_path / "legacy-tree.jsonl").exists()
    assert [message.text_content() for message in record.messages] == [
        "one",
        "two",
    ]


def test_merge_conversation_branch_preserves_abandoned_siblings() -> None:
    first = ConversationEntry(kind="user", message=Message.user("first"))
    old = ConversationEntry(
        kind="assistant",
        message=Message.assistant("old"),
        parent_id=first.id,
    )
    new = ConversationEntry(
        kind="assistant",
        message=Message.assistant("new"),
        parent_id=first.id,
    )

    merged, leaf_id = merge_conversation_branch([first, old], [first, new])

    assert leaf_id == new.id
    merged_text = {entry.message.text_content() for entry in merged if entry.message}
    assert merged_text == {
        "first",
        "old",
        "new",
    }
    active_entries = active_branch_entries(merged, leaf_id=new.id)
    assert active_entries is not None
    active_text = [
        entry.message.text_content() for entry in active_entries if entry.message
    ]
    assert active_text == [
        "first",
        "new",
    ]


def test_session_messages_only_save_preserves_off_branch_entries(
    tmp_path: Path,
) -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    old = ConversationEntry(
        kind="assistant",
        message=Message.assistant("old branch"),
        parent_id=root.id,
    )
    new = ConversationEntry(
        kind="assistant",
        message=Message.assistant("new branch"),
        parent_id=root.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-save",
        [],
        conversation_entries=[root, old, new],
        leaf_id=new.id,
    )
    record = store.load("tree-save")

    store.save("tree-save", record.messages)

    reloaded = store.load("tree-save")
    assert [entry.id for entry in reloaded.conversation_entries] == [
        root.id,
        old.id,
        new.id,
    ]
    assert reloaded.leaf_id == new.id


def test_persist_session_state_preserves_off_branch_entries(
    tmp_path: Path,
) -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    old = ConversationEntry(
        kind="assistant",
        message=Message.assistant("old branch"),
        parent_id=root.id,
    )
    new = ConversationEntry(
        kind="assistant",
        message=Message.assistant("new branch"),
        parent_id=root.id,
    )
    next_entry = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=new.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-runtime-save",
        [],
        conversation_entries=[root, old, new],
        leaf_id=new.id,
    )
    active_session = ActiveSession(
        id="tree-runtime-save",
        root=tmp_path,
        store=store,
        record=store.load("tree-runtime-save"),
    )

    persist_session_state(
        active_session,
        object(),
        [
            Message.user("root"),
            Message.assistant("new branch"),
            Message.user("continue"),
        ],
        conversation_entries=[root, new, next_entry],
    )

    saved_entry_ids = [entry.id for entry in active_session.record.conversation_entries]
    assert saved_entry_ids == [
        root.id,
        old.id,
        new.id,
        next_entry.id,
    ]
    assert active_session.record.leaf_id == next_entry.id


def test_save_agent_session_state_preserves_off_branch_entries(
    tmp_path: Path,
) -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    old = ConversationEntry(
        kind="assistant",
        message=Message.assistant("old branch"),
        parent_id=root.id,
    )
    new = ConversationEntry(
        kind="assistant",
        message=Message.assistant("new branch"),
        parent_id=root.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-agent-save",
        [],
        conversation_entries=[root, old, new],
        leaf_id=new.id,
    )
    active_session = ActiveSession(
        id="tree-agent-save",
        root=tmp_path,
        store=store,
        record=store.load("tree-agent-save"),
    )

    save_agent_session_state(
        active_session,
        AgentState(conversation_entries=[root, new]),
        leaf_id=new.id,
    )

    saved_entry_ids = [entry.id for entry in active_session.record.conversation_entries]
    assert saved_entry_ids == [
        root.id,
        old.id,
        new.id,
    ]
    assert active_session.record.leaf_id == new.id


def test_save_agent_session_state_uses_state_leaf_id(tmp_path: Path) -> None:
    root = ConversationEntry(kind="user", message=Message.user("question"))
    selected = ConversationEntry(
        kind="assistant",
        message=Message.assistant("selected branch"),
        parent_id=root.id,
    )
    later_sibling = ConversationEntry(
        kind="assistant",
        message=Message.assistant("later sibling"),
        parent_id=root.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-state-leaf",
        [],
        conversation_entries=[root, selected, later_sibling],
        leaf_id=later_sibling.id,
    )
    active_session = ActiveSession(
        id="tree-state-leaf",
        root=tmp_path,
        store=store,
        record=store.load("tree-state-leaf"),
    )

    save_agent_session_state(
        active_session,
        AgentState(
            conversation_entries=[root, selected, later_sibling],
            leaf_id=selected.id,
        ),
    )

    assert active_session.record.leaf_id == selected.id
    assert [message.text_content() for message in active_session.record.messages] == [
        "question",
        "selected branch",
    ]
