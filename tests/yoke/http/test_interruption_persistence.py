"""Interruption appends a small checkpoint without copying historical payloads."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yoke.agent.models import ConversationEntry, Message, ToolCall, ToolFunction
from yoke.agent.session_tree import ConversationProjection, SessionTree
from yoke.agent.session_tree._tool_sequence import tail_open_tool_call_ids
from yoke.ai.providers.codex.subscription.messages import convert_messages
from yoke.ai.providers.openai_compat import serialize_message_for_openai
from yoke.http.services.runtime_persistence import (
    tag_continuation_entries,
    tag_input_entry,
    with_turn_summary,
)
from yoke.http.services.session_runtime.interruption import persist_interruption
from yoke.session.admissions import INPUT_ID_METADATA_KEY, AdmissionRecord
from yoke.session.interrupt import interrupted_turn_snapshot
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for


@pytest.mark.parametrize("mode", ["missing", "persisted", "continuation", "empty"])
@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("compacted", [False, True])
def test_interruption_only_copies_and_appends_new_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    dangling: bool,
    compacted: bool,
) -> None:
    app = app_for(tmp_path, RecordingProvider())
    store = app.state.session_service.store
    tree = SessionTree.from_messages(
        []
        if mode == "empty"
        else [
            Message.user("original"),
            Message.assistant("old answer"),
        ]
    )
    if compacted and mode != "empty":
        tree.append_checkpoint(
            "retained summary", retained_messages=[Message.user("original")]
        )
    if mode == "persisted":
        tree.append_message(Message.user("correction"))
    exported = tree.export_for_persistence()
    entries = list(exported.entries)
    if mode == "persisted":
        tag_input_entry(entries, "input")
    if entries:
        # Inactive branches and large nested results must survive unchanged too.
        entries[0].metadata["historical_payload"] = [{"nested": list(range(100))}]
        entries.append(
            ConversationEntry(
                kind="assistant",
                message=Message.assistant("inactive branch"),
                parent_id=entries[0].id,
            )
        )
    record = store.save(
        "session-a",
        [],
        conversation_entries=entries,
        leaf_id=exported.leaf_id,
        root=tmp_path,
        title="Preserved title",
        provider_name="fixture",
        model_id="model",
    )
    if dangling and mode != "empty":
        # Ordinary saves normalize incomplete batches; runtime checkpoints may
        # legitimately end with an indexed, still-open tool call.
        call = ConversationEntry(
            kind="assistant_tool_calls",
            parent_id=record.leaf_id,
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="unfinished",
                        function=ToolFunction(name="read", arguments="{}"),
                    ),
                ],
            ),
        )
        store.save_indexed_tree_navigation(
            "session-a",
            existing_record=record,
            leaf_id=call.id,
            appended_entries=(call,),
            clear_context_usage=False,
        )
    admission = AdmissionRecord(
        id="input",
        session_id="session-a",
        prompt="correction",
        continuation=mode == "continuation",
        delivery="queue",
        fingerprint="fixture",
        time_created="2026-09-29T00:00:00+00:00",
        admitted_seq=1,
    )
    with TestClient(app):
        runtime = app.state.runtime_registry.get_or_start("session-a")
        snapshot = runtime._snapshot()
        if dangling and mode != "empty":
            assert tail_open_tool_call_ids(snapshot.active_path_entries) == (
                "unfinished",
            )
        before = [entry.model_dump() for entry in snapshot.record.conversation_entries]
        prefix = store.directory.joinpath("session-a.jsonl").read_bytes()
        # Reference the original full-history algorithm before installing guards.
        active = snapshot.owned_active_path()
        _, expected = interrupted_turn_snapshot(
            messages=(),
            entries=active,
            leaf_id=snapshot.record.leaf_id,
            user_message=Message.user("correction")
            if mode in {"missing", "empty"}
            else None,
        )
        expected = with_turn_summary(expected, duration_seconds=120, tool_count=3)
        if admission.continuation:
            expected = tag_continuation_entries(
                expected, admission.id, {entry.id for entry in active}
            )
        else:
            tag_input_entry(expected, admission.id)
        expected_messages = (
            SessionTree.restore(expected, expected[-1].id)
            .project(ConversationProjection())
            .provider_messages
        )
        historical_ids = {entry.id for entry in snapshot.record.conversation_entries}
        original_copy = ConversationEntry.model_copy

        def guard_copy(self, *, update=None, deep=False):
            assert not (deep and self.id in historical_ids), (
                "interruption copied historical payloads"
            )
            return original_copy(self, update=update, deep=deep)

        def reject_full_save(*_args, **_kwargs):
            raise AssertionError(
                "interruption rewrote full history instead of appending its suffix"
            )

        with monkeypatch.context() as guarded:
            guarded.setattr(ConversationEntry, "model_copy", guard_copy)
            guarded.setattr(runtime, "_save_entries_locked", reject_full_save)
            persist_interruption(runtime, admission, duration_seconds=120, tool_count=3)

        assert [
            entry.model_dump() for entry in snapshot.record.conversation_entries
        ] == before
        saved = store.load("session-a")
        assert [
            entry.model_dump() for entry in saved.conversation_entries[: len(before)]
        ] == before
        projected = SessionTree.restore(
            saved.conversation_entries, saved.leaf_id
        ).project(ConversationProjection())
        assert [message.model_dump() for message in projected.provider_messages] == [
            message.model_dump() for message in expected_messages
        ]
        # Legacy full-history saves normalized null assistant content to empty
        # strings. Preserve exact wire requests without rewriting those rows.
        legacy_messages = [
            message.model_copy(update={"content": ""})
            if message.role == "assistant" and message.content is None
            else message
            for message in expected_messages
        ]
        assert convert_messages(list(projected.provider_messages)) == convert_messages(
            legacy_messages
        )
        assert [
            serialize_message_for_openai(m) for m in projected.provider_messages
        ] == [serialize_message_for_openai(m) for m in legacy_messages]
        actual_suffix = saved.conversation_entries[len(before) :]
        if dangling and mode != "empty":
            assert actual_suffix[0].metadata["recovered_incomplete_tool_call"] is True
            assert actual_suffix[0].message.tool_call_id == "unfinished"
        expected_suffix = expected[len(active) :]
        assert [(e.kind, e.message, e.metadata) for e in actual_suffix] == [
            (e.kind, e.message, e.metadata) for e in expected_suffix
        ]
        assert actual_suffix[0].parent_id == snapshot.record.leaf_id
        assert all(
            b.parent_id == a.id for a, b in zip(actual_suffix, actual_suffix[1:])
        )
        assert saved.leaf_id == actual_suffix[-1].id
        assert saved.title == "Preserved title"
        assert saved.provider_name == "fixture"
        assert saved.model_id == "model"
        assert (
            store.directory.joinpath("session-a.jsonl").read_bytes().startswith(prefix)
        )
        assert store.index_entry("session-a").entry_count == len(
            saved.conversation_entries
        )
        assert runtime._snapshot().record.leaf_id == saved.leaf_id
        tagged_users = [
            e
            for e in saved.conversation_entries
            if e.kind == "user" and e.metadata.get(INPUT_ID_METADATA_KEY) == "input"
        ]
        assert len(tagged_users) == (0 if admission.continuation else 1)
