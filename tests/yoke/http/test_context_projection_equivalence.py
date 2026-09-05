from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_core import to_json

from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import InvalidCheckpointError
from yoke.agent.session_tree import MissingParentError
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.cli.session.io import SESSION_METADATA_EVENT
from yoke.http.services.projectors import project_message_content
from yoke.http.services.session_service import SessionService
from yoke.session import SessionStore

# Indexed Context remains a bounded approximation with different checkpoint and
# metadata behavior. These regressions use the saved-tree projection as the
# fallback oracle rather than claiming indexed/fallback parity.


def _representative_tree() -> SessionTree:
    retained_image = Message.user(
        [
            MessageTextContentPart(text="retained request"),
            MessageLocalImageContentPart(
                path="/private/worktree/retained.png",
                label="retained image",
            ),
        ]
    )
    tree = SessionTree.from_messages(
        [
            Message.system("base instruction"),
            retained_image,
            Message.assistant("historical answer"),
        ]
    )
    branch_point = tree.current
    assert branch_point is not None
    tree.append_system_event(Message.system("abandoned skill"))
    tree.append_message(Message.assistant("abandoned branch"))
    tree.checkout(branch_point)
    tree.append_system_event(Message.system("active review skill"))
    tree.append_message(Message.user("pre-checkpoint request"))
    tree.append_checkpoint(
        "compacted state",
        retained_messages=[retained_image, Message.user("handoff request")],
    )
    call = ToolCall(
        id="read-1",
        function=ToolFunction(name="read", arguments='{"path":"README.md"}'),
    )
    tree.append_message(
        Message(
            role="assistant",
            content="checking",
            tool_calls=[call],
            phase="commentary",
        )
    )
    tree.append_message(Message.tool(call.id, '{"result":"ok"}'))
    tree.append_tool_context(
        Message.user(
            [
                MessageTextContentPart(text="tool verification"),
                MessageImageURLContentPart(
                    image_url=MessageImageURL(url="data:image/png;base64,VERIFICATION"),
                    label="verification image",
                ),
            ]
        ),
        metadata={"tool_name": "read", "tool_call_id": call.id},
    )
    tree.append_message(Message.assistant("final answer", phase="final_answer"))
    return tree


def _save_tree(store: SessionStore, session_id: str, tree: SessionTree, root: Path):
    exported = tree.export_for_persistence()
    return store.save(
        session_id,
        [entry.message for entry in exported.entries if entry.message is not None],
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
    )


@pytest.mark.parametrize("include_system", [False, True])
@pytest.mark.parametrize("include_tool_results", [False, True])
def test_forced_fallback_matches_canonical_saved_tree_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_system: bool,
    include_tool_results: bool,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    tree = _representative_tree()
    record = _save_tree(store, "context-session", tree, tmp_path)
    service = SessionService(store)
    monkeypatch.setattr(
        service.message_index,
        "context_window",
        lambda *_args, **_kwargs: None,
    )
    try:
        response = service.context(
            "context-session",
            include_system=include_system,
            include_tool_results=include_tool_results,
            limit=100,
            max_chars=500_000,
        )
    finally:
        service.close()

    view = SessionTree.restore(
        record.conversation_entries,
        record.leaf_id,
    ).project(ConversationProjection())
    expected = list(view.provider_messages)
    if include_system:
        expected = [
            *(
                entry.message
                for entry in view.active_entries
                if entry.kind == "instruction" and entry.message is not None
            ),
            *expected,
        ]
    expected = [
        message
        for message in expected
        if (include_system or message.role != "system")
        and (include_tool_results or message.role != "tool")
    ]
    assert [
        (
            message.role,
            [part.model_dump(mode="json") for part in message.content],
            message.tool_call_id,
            message.phase,
        )
        for message in response.data.messages
    ] == [
        (
            message.role,
            [part.model_dump(mode="json") for part in project_message_content(message)],
            message.tool_call_id,
            message.phase,
        )
        for message in expected
    ]
    assert response.data.total_entries == len(view.active_entries)
    assert response.data.retained_entries == len(view.active_entries)
    assert response.data.truncated is False
    serialized = response.model_dump_json(by_alias=True)
    assert "/private/worktree" not in serialized
    assert "abandoned branch" not in serialized
    assert "abandoned skill" not in serialized


def test_forced_fallback_uses_last_entry_when_saved_leaf_is_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    tree = SessionTree.from_messages(
        [Message.user("first"), Message.assistant("second")]
    )
    _save_tree(store, "null-leaf", tree, tmp_path)
    path = store.directory / "null-leaf.jsonl"
    with path.open("ab") as handle:
        handle.write(to_json({"type": SESSION_METADATA_EVENT, "leaf_id": None}))
        handle.write(b"\n")

    service = SessionService(store)
    monkeypatch.setattr(
        service.message_index,
        "context_window",
        lambda *_args, **_kwargs: None,
    )
    try:
        response = service.context(
            "null-leaf",
            include_system=False,
            include_tool_results=True,
            limit=10,
            max_chars=10_000,
        )
    finally:
        service.close()

    assert [
        part.text
        for message in response.data.messages
        for part in message.content
        if part.type == "text"
    ] == ["first", "second"]
    assert response.data.total_entries == 2


def test_forced_fallback_rejects_corrupt_older_applicable_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    tree = SessionTree.from_messages([Message.user("first")])
    tree.append_checkpoint("older summary")
    tree.append_message(Message.user("continued"))
    tree.append_checkpoint("newer summary")
    tree.append_message(Message.assistant("done"))
    exported = tree.export_for_persistence()
    entries = list(exported.entries)
    older = next(entry for entry in entries if entry.kind == "memory_snapshot")
    older.metadata = {"id": "missing-summary"}
    store.save(
        "corrupt-checkpoint",
        [entry.message for entry in entries if entry.message is not None],
        conversation_entries=entries,
        leaf_id=exported.leaf_id,
        root=tmp_path,
    )
    service = SessionService(store)
    monkeypatch.setattr(
        service.message_index,
        "context_window",
        lambda *_args, **_kwargs: None,
    )
    try:
        with pytest.raises(InvalidCheckpointError, match="is invalid"):
            service.context(
                "corrupt-checkpoint",
                include_system=False,
                include_tool_results=True,
                limit=10,
                max_chars=10_000,
            )
    finally:
        service.close()


def test_forced_fallback_rejects_invalid_saved_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    tree = SessionTree.from_messages(
        [Message.user("first"), Message.assistant("second")]
    )
    _save_tree(store, "invalid-topology", tree, tmp_path)
    record = store.load("invalid-topology")
    record.conversation_entries[-1].parent_id = "missing-parent"
    service = SessionService(store)
    monkeypatch.setattr(
        service.message_index,
        "context_window",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "_require_snapshot",
        lambda _session_id: SimpleNamespace(record=record),
    )
    try:
        with pytest.raises(MissingParentError, match="missing parent"):
            service.context(
                "invalid-topology",
                include_system=False,
                include_tool_results=True,
                limit=10,
                max_chars=10_000,
            )
    finally:
        service.close()


def test_truncated_cold_context_uses_bounded_tail_without_topology_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    messages = [
        message
        for index in range(100)
        for message in (
            Message.user(f"user {index}"),
            Message.assistant(f"assistant {index}"),
        )
    ]
    store.save("cold-context", messages, root=tmp_path)
    service = SessionService(store)

    def fail(*_args, **_kwargs):
        raise AssertionError("cold bounded Context must not build full topology")

    monkeypatch.setattr(service.message_index, "_ensure", fail)
    monkeypatch.setattr(service.message_index, "warm_async", lambda *_args: None)
    monkeypatch.setattr(store, "load", fail)
    try:
        response = service.context(
            "cold-context",
            include_system=False,
            include_tool_results=True,
            limit=2,
            max_chars=10_000,
        )
    finally:
        service.close()

    assert [
        part.text
        for message in response.data.messages
        for part in message.content
        if part.type == "text"
    ] == ["user 99", "assistant 99"]
    assert response.data.total_entries == 200
    assert response.data.retained_entries == 2
    assert response.data.truncated is True
