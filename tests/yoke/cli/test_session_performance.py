from __future__ import annotations

# ruff: noqa: ANN401,D100,D103,D105,S101

from collections.abc import Iterator
import json
from pathlib import Path
from threading import Lock
from typing import Any, cast

import pytest

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.cancellation import (
    interrupted_turn_snapshot,
)
from yoke.cli.interactive.prompt.loop import persist_prompt_exit_state
from yoke.cli.interactive.prompt.turns import prepare_turn_agent
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import tree_view
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime.session import save_active_session
from yoke.cli.runtime.tree import flatten_tree_rows
from yoke.cli.runtime.tree import get_session_tree
from yoke.cli.runtime.tree import default_folded_tree_ids
from yoke.cli.runtime.tree import set_entry_label
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore
from yoke.cli.session.io import decode_session_record
from yoke.cli.session.io import record_jsonl

ENTRY_COUNT = 50_000
ACTIVE_COUNT = 32


class ScanCountingEntries(list[ConversationEntry]):
    """Count complete entry-list iterations without using wall time."""

    scans: int = 0

    def __iter__(self) -> Iterator[ConversationEntry]:
        self.scans += 1
        return super().__iter__()


class NoopProvider:
    """Return one final message without network work."""

    supports_image_inputs = False
    max_images_per_message = None
    provider_name = "noop"

    def complete(self, messages, tools) -> Message:
        """Return one fixed response."""
        del messages, tools
        return Message.assistant("done")


@pytest.fixture(scope="module")
def large_tree() -> tuple[list[ConversationEntry], str]:
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for index in range(ACTIVE_COUNT):
        entry = ConversationEntry(
            id=f"active-{index:08d}",
            kind="control",
            parent_id=parent_id,
        )
        entries.append(entry)
        parent_id = entry.id
    inactive_parent_id = entries[0].id
    for index in range(ACTIVE_COUNT, ENTRY_COUNT):
        entry = ConversationEntry(
            id=f"inactive-{index:08d}",
            kind="user" if index == ACTIVE_COUNT else "control",
            message=(
                Message.user("inactive branch") if index == ACTIVE_COUNT else None
            ),
            parent_id=inactive_parent_id,
        )
        entries.append(entry)
        inactive_parent_id = entry.id
    assert parent_id is not None
    return entries, parent_id


def _record_with_counted_entries(
    entries: list[ConversationEntry], leaf_id: str
) -> tuple[SessionRecord, ScanCountingEntries]:
    counted = ScanCountingEntries(entries)
    record = SessionRecord.model_construct(
        id="large-session",
        conversation_entries=counted,
        leaf_id=leaf_id,
    )
    return record, counted


def _count_entry_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    copies = {"count": 0}
    original = ConversationEntry.model_copy

    def counted_copy(
        self: ConversationEntry, *args: Any, **kwargs: Any
    ) -> ConversationEntry:
        copies["count"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConversationEntry, "model_copy", counted_copy)
    return copies


def test_retained_index_scans_once_and_copies_only_the_active_path(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path),
        record,
    )
    validation_scans = counted.scans
    copies = _count_entry_copies(monkeypatch)

    first = active.active_entries()
    second = active.active_entries()

    assert validation_scans == 2
    assert counted.scans == validation_scans
    assert len(first) == ACTIVE_COUNT
    assert len(second) == ACTIVE_COUNT
    assert copies["count"] == ACTIVE_COUNT * 2


def test_interruption_snapshot_reuses_active_history(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path),
        record,
    )
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)

    messages, snapshot = interrupted_turn_snapshot(
        messages=[],
        entries=active.active_entry_refs(),
        user_message=Message.user("steer"),
    )

    assert counted.scans == scans_before
    assert copies["count"] == 0
    assert len(messages) == 2
    assert len(snapshot) == ACTIVE_COUNT + 2
    assert snapshot[-3] is active.tree_index.active_entry_refs()[-1]


def test_interruption_checkpoint_copies_only_the_synthetic_suffix(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    store = SessionStore(tmp_path / "sessions")
    store._write_session_record(record)
    active = ActiveSession(record.id, tmp_path, store, record)
    messages, snapshot = interrupted_turn_snapshot(
        messages=[],
        entries=active.active_entry_refs(),
        user_message=Message.user("stop"),
    )
    agent = RuntimeAgent(provider=NoopProvider(), tools=[])
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("interruption checkpoint reconciled the complete tree")

    monkeypatch.setattr("yoke.agent.session_tree.SessionTree.reconcile", fail)

    try:
        persist_session_state(
            active,
            agent,
            messages,
            conversation_entries=snapshot,
        )
    finally:
        agent.close()

    assert counted.scans == scans_before
    assert copies["count"] < 10
    assert active.record.conversation_entries[-2].kind == "user"
    assert active.record.conversation_entries[-1].kind == "assistant"


def test_turn_preparation_takes_the_validated_active_path(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, _ = _record_with_counted_entries(entries, leaf_id)
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path),
        record,
    )
    transferred = active.active_entries()
    agent = RuntimeAgent(provider=NoopProvider(), tools=[])
    copies = _count_entry_copies(monkeypatch)
    turn = prepare_turn_agent(agent, messages=[], entries=transferred)
    runtime_turn = cast(RuntimeAgent, turn)
    try:
        assert isinstance(turn, RuntimeAgent)
        assert copies["count"] == 0
        assert runtime_turn._context is not None
        runtime_entries = runtime_turn._context.conversation_log.entries
        assert len(runtime_entries) == ACTIVE_COUNT
        assert runtime_entries[0] is transferred[0]
    finally:
        runtime_turn.close()
        agent.close()


def test_runtime_append_copies_only_the_new_entry(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, _ = _record_with_counted_entries(entries, leaf_id)
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path),
        record,
    )
    manager = ContextManager()
    context = manager.initialize_owned(
        "",
        active.active_entries(),
        append_prompt=False,
    )
    copies = _count_entry_copies(monkeypatch)

    manager.append_message(context, Message.user("new"))

    assert copies["count"] == 1
    assert len(context.conversation_log.entries) == ACTIVE_COUNT + 1
    assert context.conversation_log.entries[-1].parent_id == leaf_id


def test_proven_save_appends_without_tree_scan_or_prefix_comparison(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    store = SessionStore(tmp_path / "sessions")
    store._write_session_record(record)
    active = ActiveSession(record.id, tmp_path, store, record)
    incoming = active.active_entries()
    appended = ConversationEntry(
        id="appended-entry",
        kind="user",
        message=Message.user("new"),
        parent_id=leaf_id,
    )
    incoming.append(appended)
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)
    committed: list[SessionRecord] = []
    original_save = store.save

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("full-tree fallback was used")

    def track_save(*args: Any, **kwargs: Any) -> SessionRecord:
        saved = original_save(*args, **kwargs)
        committed.append(saved)
        return saved

    monkeypatch.setattr("yoke.cli.session.tree.SessionTree.restore", fail)
    monkeypatch.setattr("yoke.cli.session.writer.append_jsonl_lines", fail)
    monkeypatch.setattr(store, "load", fail)
    monkeypatch.setattr(store, "_prune_index_and_sessions", fail)
    monkeypatch.setattr(store, "save", track_save)

    save_active_session(
        active,
        [Message.user("new")],
        conversation_entries=incoming,
    )

    assert committed == [active.record]
    assert counted.scans == scans_before
    assert copies["count"] < 10
    assert active.record.conversation_entries[-1].id == appended.id
    assert active.tree_index.active_entry_refs()[-1].id == appended.id
    with store._session_path(record.id).open(encoding="utf-8") as handle:
        last_event = json.loads(list(handle)[-1])
    assert last_event["entry"]["id"] == appended.id


def test_clean_prompt_exit_does_not_capture_or_save_conversation(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    record.root = str(tmp_path.resolve())
    record.provider_name = "noop"
    record.created_at = "2026-08-03T00:00:00+00:00"
    record.updated_at = record.created_at
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path / "sessions"),
        record,
    )
    state = PromptCliState(messages=[], pending_prompts=[])
    agent = RuntimeAgent(provider=NoopProvider(), tools=[])
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("clean exit processed the conversation tree")

    monkeypatch.setattr("yoke.cli.runtime.session.capture_agent_state", fail)
    monkeypatch.setattr(active.store, "save", fail)
    monkeypatch.setattr(active.store, "_write_session_record", fail)

    try:
        persist_prompt_exit_state(
            state=state,
            state_lock=Lock(),
            active_session=active,
            agent=agent,
        )
    finally:
        agent.close()

    assert counted.scans == scans_before
    assert copies["count"] == 0


def test_prompt_exit_appends_only_changed_reasoning_metadata(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    record.root = str(tmp_path.resolve())
    record.provider_name = "noop"
    record.reasoning_effort = "low"
    record.created_at = "2026-08-03T00:00:00+00:00"
    record.updated_at = record.created_at
    store = SessionStore(tmp_path / "sessions")
    store.directory.mkdir(parents=True)
    store._session_path(record.id).touch()
    active = ActiveSession(record.id, tmp_path, store, record)
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        thinking_effort="medium",
    )
    agent = RuntimeAgent(provider=NoopProvider(), tools=[])
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)
    events: list[dict[str, object]] = []

    def capture_metadata(_path: Path, changes: dict[str, object]) -> None:
        events.append(changes)

    monkeypatch.setattr(
        "yoke.cli.session.metadata.append_session_metadata",
        capture_metadata,
    )
    monkeypatch.setattr(store, "_update_index", lambda _record: None)

    try:
        persist_prompt_exit_state(
            state=state,
            state_lock=Lock(),
            active_session=active,
            agent=agent,
        )
    finally:
        agent.close()

    assert counted.scans == scans_before
    assert copies["count"] == 0
    assert len(events) == 1
    assert events[0]["reasoning_effort"] == "medium"
    assert active.record.reasoning_effort == "medium"


def test_unchanged_transcript_persistence_uses_retained_active_path(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    store = SessionStore(tmp_path / "sessions")
    store._write_session_record(record)
    active = ActiveSession(record.id, tmp_path, store, record)
    messages = active.messages()
    agent = RuntimeAgent(provider=NoopProvider(), tools=[])
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unchanged transcript used full-tree reconciliation")

    monkeypatch.setattr("yoke.agent.session_tree.SessionTree.reconcile", fail)
    monkeypatch.setattr(store, "load", fail)
    monkeypatch.setattr(store, "_prune_index_and_sessions", fail)

    try:
        persist_session_state(active, agent, messages)
    finally:
        agent.close()

    assert counted.scans == scans_before
    assert copies["count"] <= ACTIVE_COUNT


def test_tree_selector_build_and_filter_are_single_pass(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    active = ActiveSession(
        record.id,
        tmp_path,
        SessionStore(tmp_path),
        record,
    )
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)
    filter_calls = {"count": 0}
    original_filter = tree_view._filter_matches

    def counted_filter(*args: Any, **kwargs: Any) -> bool:
        filter_calls["count"] += 1
        if filter_calls["count"] > ENTRY_COUNT:
            pytest.fail("tree filtering scanned a subtree more than once")
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(tree_view, "_filter_matches", counted_filter)

    roots = get_session_tree(active)
    folded = default_folded_tree_ids(roots)
    rows = flatten_tree_rows(
        roots,
        current_leaf_id=leaf_id,
        folded_ids=folded,
    )

    assert counted.scans == scans_before + 1
    assert copies["count"] == 0
    assert filter_calls["count"] == 0
    searched = flatten_tree_rows(
        roots,
        current_leaf_id=leaf_id,
        search="not-present",
    )
    assert filter_calls["count"] == 1
    assert folded == {f"inactive-{ACTIVE_COUNT:08d}"}
    assert [row.entry.id for row in rows] == [
        f"inactive-{ACTIVE_COUNT:08d}",
        leaf_id,
    ]
    assert [row.entry.id for row in searched] == [leaf_id]


def test_store_load_streams_without_path_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    record = SessionRecord(
        id="streamed",
        conversation_entries=[
            ConversationEntry(id="one", kind="user", message=Message.user("x"))
        ],
        leaf_id="one",
    )
    store._write_session_record(record)

    def fail_read_text(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Path.read_text loaded the complete session")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    loaded = store.load(record.id)

    assert loaded.conversation_entries[0].id == "one"


def test_tree_label_appends_one_indexed_entry_update(
    large_tree: tuple[list[ConversationEntry], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, leaf_id = large_tree
    record, counted = _record_with_counted_entries(entries, leaf_id)
    store = SessionStore(tmp_path / "sessions")
    store._write_session_record(record)
    active = ActiveSession(record.id, tmp_path, store, record)
    path = store._session_path(record.id)
    size_before = path.stat().st_size
    scans_before = counted.scans
    copies = _count_entry_copies(monkeypatch)

    def fail_rewrite(*_args: object, **_kwargs: object) -> None:
        pytest.fail("label update rewrote the complete session")

    monkeypatch.setattr(store, "_write_session_record", fail_rewrite)

    set_entry_label(active, leaf_id, "checkpoint")

    assert counted.scans == scans_before + 2
    assert copies["count"] == 2
    assert path.stat().st_size - size_before < 1_000
    assert record.conversation_entries[ACTIVE_COUNT - 1].metadata == {}
    loaded = store.load(record.id)
    assert loaded.conversation_entries[ACTIVE_COUNT - 1].metadata == {
        "label": "checkpoint"
    }
    assert loaded.conversation_entries[ACTIVE_COUNT - 1].id == leaf_id


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
        record_jsonl(record)
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
