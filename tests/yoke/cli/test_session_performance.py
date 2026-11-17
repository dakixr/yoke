from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

import json
from pathlib import Path
from threading import Lock

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.session_tree import SessionTree
from yoke.agent.loop import RuntimeAgent
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.loop import persist_prompt_exit_state
from yoke.session.repository import SessionRecord
from yoke.session import SessionStore
from yoke.session.legacy.jsonl import decode_session_record
from yoke.session.repository.table import SessionTable

from tests.yoke.session_rows import stored_entry_rows
from tests.yoke.session_support import StoredSession
from .terminal_daemon_support import terminal as terminal, session_for
from .test_cli_model_switching import SwitchableProvider


class NoopProvider:
    """Return one final message without network work."""

    supports_image_inputs = False
    max_images_per_message = None
    provider_name = "noop"

    def complete(self, messages, tools) -> Message:
        """Return one fixed response."""
        del messages, tools
        return Message.assistant("done")


def _legacy_jsonl(record: SessionRecord) -> str:
    """Encode a record the way the JSONL store wrote it."""
    lines = [
        {"type": "yoke_session", "version": 2},
        {
            "type": "metadata",
            **record.model_dump(mode="json", exclude={"conversation_entries"}),
        },
        *(
            {"type": "entry", "entry": entry.model_dump(mode="json")}
            for entry in record.conversation_entries
        ),
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def _large_record(tmp_path: Path) -> SessionRecord:
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for index in range(32):
        entry = ConversationEntry(
            id=f"active-{index:08d}",
            kind="control",
            parent_id=parent_id,
        )
        entries.append(entry)
        parent_id = entry.id
    assert parent_id is not None
    record = SessionRecord(
        id="large-session",
        conversation_entries=entries,
        leaf_id=parent_id,
    )
    record.root = str(tmp_path)
    record.created_at = "2099-01-01T00:00:00+00:00"
    record.updated_at = record.created_at
    return record


def test_clean_prompt_exit_does_not_capture_or_save_conversation(
    tmp_path, monkeypatch, terminal
):
    record = _large_record(tmp_path)
    record.provider_name = "demo"
    record.model_id = "gpt-a"
    record.reasoning_effort = "low"
    terminal.store.insert_record(record)
    terminal.factory = lambda _record: RuntimeAgent(
        provider=SwitchableProvider(), tools=[]
    )
    backend = terminal.backend(record.id, root=tmp_path)
    state = PromptCliState(messages=[], pending_prompts=[], thinking_effort=None)
    rows_before = stored_entry_rows(terminal.store, record.id)
    events = []
    original = SessionStore.set_selection

    def set_selection(self, session_id, **kwargs):
        events.append(kwargs)
        return original(self, session_id, **kwargs)

    monkeypatch.setattr(SessionStore, "set_selection", set_selection)

    def fail(*_args, **_kwargs):
        pytest.fail("prompt exit saved the conversation tree")

    monkeypatch.setattr(SessionStore, "save", fail)
    monkeypatch.setattr(SessionStore, "load", fail)
    persist_prompt_exit_state(
        state=state,
        state_lock=Lock(),
        active_session=session_for(backend),
        agent=backend,
    )
    assert stored_entry_rows(terminal.store, record.id) == rows_before
    assert events == []


def test_prompt_exit_writes_only_changed_reasoning_metadata(
    tmp_path, monkeypatch, terminal
):
    record = _large_record(tmp_path)
    record.provider_name = "demo"
    record.model_id = "gpt-a"
    record.reasoning_effort = "low"
    terminal.store.insert_record(record)
    terminal.factory = lambda _record: RuntimeAgent(
        provider=SwitchableProvider(), tools=[]
    )
    backend = terminal.backend(record.id, root=tmp_path)
    state = PromptCliState(messages=[], pending_prompts=[], thinking_effort="medium")
    rows_before = stored_entry_rows(terminal.store, record.id)
    events = []
    original = SessionStore.set_selection

    def set_selection(self, session_id, **kwargs):
        events.append(kwargs)
        return original(self, session_id, **kwargs)

    monkeypatch.setattr(SessionStore, "set_selection", set_selection)

    def fail(*_args, **_kwargs):
        pytest.fail("prompt exit saved the conversation tree")

    monkeypatch.setattr(SessionStore, "save", fail)
    monkeypatch.setattr(SessionStore, "load", fail)
    persist_prompt_exit_state(
        state=state,
        state_lock=Lock(),
        active_session=session_for(backend),
        agent=backend,
    )
    assert stored_entry_rows(terminal.store, record.id) == rows_before
    assert len(events) == 1
    assert events[0]["reasoning_effort"] == "medium"
    assert backend.session.record.reasoning_effort == "medium"
    assert terminal.store.summary_record(record.id).reasoning_effort == "medium"


def test_tree_label_writes_one_indexed_entry_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    record = _large_record(tmp_path)
    leaf_id = record.leaf_id
    assert leaf_id is not None
    store = SessionStore(tmp_path / "sessions")
    store.insert_record(record)
    active = StoredSession(record.id, tmp_path, store, record)
    rows_before = stored_entry_rows(store, record.id)

    def fail_rewrite(*_args: object, **_kwargs: object) -> None:
        pytest.fail("label update rewrote the complete session")

    monkeypatch.setattr(SessionTable, "write_record", fail_rewrite)

    tree = SessionTree.borrow_validated(
        active.record.conversation_entries, active.record.leaf_id
    )
    tree.set_label(tree.ref_from_persisted_id(leaf_id), "checkpoint")
    entry = tree.export_entry_for_persistence(leaf_id)
    active.record = store.save_entry_metadata(
        active.id, entry, existing_record=active.record, tree_index=active.tree_index
    )

    rows_after = stored_entry_rows(store, record.id)
    changed = [
        index for index, row in enumerate(rows_after) if row != rows_before[index]
    ]
    assert len(rows_after) == len(rows_before)
    assert [rows_after[index][1] for index in changed] == [leaf_id]
    assert record.conversation_entries[31].metadata == {}
    loaded = store.load(record.id)
    assert loaded.conversation_entries[31].metadata == {"label": "checkpoint"}
    assert loaded.conversation_entries[31].id == leaf_id


def test_duplicate_entry_replacement_uses_latest_event_order() -> None:
    first = ConversationEntry(id="first", kind="control")
    second = ConversationEntry(id="second", kind="control")
    record = SessionRecord(
        id="duplicates",
        conversation_entries=[first, second],
        leaf_id=second.id,
    )
    replacement = first.model_copy(update={"metadata": {"replacement": True}})
    raw = (
        _legacy_jsonl(record)
        + json.dumps(
            {
                "type": "entry",
                "entry": replacement.model_dump(mode="json"),
            }
        )
        + "\n"
    )

    decoded = decode_session_record(raw)

    assert [entry.id for entry in decoded.conversation_entries] == [
        "second",
        "first",
    ]
    assert decoded.conversation_entries[-1].metadata == {"replacement": True}
