from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import write_prompt_queue_snapshot


TOKEN = "session-view-test"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            HttpAppSettings(
                auth_token=TOKEN,
                session_directory=tmp_path / "sessions",
            )
        )
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _state(client: TestClient):  # noqa: ANN202
    return cast(FastAPI, client.app).state


def _legacy_attach_image_tree(*, include_private: bool = True) -> SessionTree:
    call = ToolCall(
        id="attach-legacy",
        function=ToolFunction(name="attach_image", arguments="{}"),
    )
    tree = SessionTree.from_messages([Message.user("inspect")])
    tree.append_message(Message(role="assistant", content=None, tool_calls=[call]))
    tree.append_message(Message.tool(call.id, '{"ok":true}'))
    if include_private:
        tree.append_message(
            Message(
                role="user",
                content=[
                    MessageTextContentPart(text="private legacy verification"),
                    MessageImageURLContentPart(
                        image_url=MessageImageURL(
                            url="data:image/png;base64,PRIVATE_LEGACY_IMAGE"
                        )
                    ),
                ],
            )
        )
        tree.append_message(Message.assistant("verified"))
    return tree


def _save_tree(state, session_id: str, tree: SessionTree, root: Path, **kwargs):  # noqa: ANN001,ANN202
    exported = tree.export_for_persistence()
    record = state.session_service.store.save(
        session_id,
        [entry.message for entry in exported.entries if entry.message is not None],
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
        **kwargs,
    )
    return record, exported


def _paginate_messages(
    client: TestClient,
    session_id: str,
    *,
    order: str,
) -> tuple[list[str], str]:
    seen: list[str] = []
    bodies: list[str] = []
    cursor = None
    while True:
        params = {"limit": 1, "order": order}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(
            f"/api/v1/session/{session_id}/message",
            headers=_auth(),
            params=params,
        )
        assert response.status_code == 200
        bodies.append(response.text)
        seen.extend(item["id"] for item in response.json()["data"])
        cursor = response.json()["cursor"]["next"]
        if cursor is None:
            return seen, "".join(bodies)


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_forced_message_fallback_filters_tool_context_before_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    call = ToolCall(
        id="attach-1",
        function=ToolFunction(name="attach_image", arguments="{}"),
    )
    tree = SessionTree.from_messages([Message.user("inspect")])
    tree.append_message(Message(role="assistant", content=None, tool_calls=[call]))
    tree.append_message(Message.tool(call.id, '{"ok":true}'))
    tree.append_tool_context(
        Message.user(
            [
                MessageTextContentPart(text="private verification context"),
                MessageImageURLContentPart(
                    image_url=MessageImageURL(
                        url="data:image/png;base64,PRIVATE_IMAGE_BYTES"
                    )
                ),
            ]
        ),
        metadata={"tool_name": "attach_image", "tool_call_id": call.id},
    )
    tree.append_message(Message.assistant("verified"))
    tree.append_message(Message.user("continue"))
    exported = tree.export_for_persistence()
    state = _state(client)
    state.session_service.store.save(
        "privacy-session",
        [entry.message for entry in exported.entries if entry.message is not None],
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
    )
    public_ids = [
        entry.id for entry in exported.entries if entry.kind != "tool_context"
    ]
    if order == "desc":
        public_ids.reverse()

    monkeypatch.setattr(
        state.session_service.message_index,
        "page",
        lambda *_args, **_kwargs: None,
    )
    seen: list[str] = []
    cursor = None
    responses = []
    while True:
        params = {"limit": 2, "order": order}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(
            "/api/v1/session/privacy-session/message",
            headers=_auth(),
            params=params,
        )
        assert response.status_code == 200
        responses.append(response.text)
        seen.extend(item["id"] for item in response.json()["data"])
        cursor = response.json()["cursor"]["next"]
        if cursor is None:
            break

    assert seen == public_ids
    assert len(seen) == len(set(seen))
    assert "private verification context" not in "".join(responses)
    assert "PRIVATE_IMAGE_BYTES" not in "".join(responses)

    context = client.get(
        "/api/v1/session/privacy-session/context",
        headers=_auth(),
    )
    assert context.status_code == 200
    assert "private verification context" in context.text
    assert "PRIVATE_IMAGE_BYTES" in context.text
    inspected_tree = client.get(
        "/api/v1/session/privacy-session/tree",
        headers=_auth(),
        params={"limit": 20},
    )
    assert inspected_tree.status_code == 200
    assert "tool_context" in [
        entry["kind"] for entry in inspected_tree.json()["data"]["entries"]
    ]


def test_cold_small_pages_classify_legacy_tool_context_before_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    state = _state(client)
    tree = _legacy_attach_image_tree()
    _, exported = _save_tree(state, "cold-legacy", tree, root)
    private_id = next(
        entry.id
        for entry in exported.entries
        if entry.message is not None
        and "private legacy verification"
        in (entry.message.display_text_content() or "")
    )
    expected = [
        entry.id for entry in reversed(exported.entries) if entry.id != private_id
    ]
    monkeypatch.setattr(
        state.session_service.message_index,
        "warm_async",
        lambda *_args: None,
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("cold legacy pagination must stay on the bounded tail")

    monkeypatch.setattr(state.session_service.message_index, "_ensure", fail)

    seen, bodies = _paginate_messages(client, "cold-legacy", order="desc")

    assert seen == expected
    assert len(seen) == len(set(seen))
    assert "private legacy verification" not in bodies
    assert "PRIVATE_LEGACY_IMAGE" not in bodies


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_warm_index_reclassifies_appended_legacy_tool_context(
    tmp_path: Path,
    order: str,
) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    state = _state(client)
    tree = _legacy_attach_image_tree(include_private=False)
    record, _ = _save_tree(state, "warm-legacy", tree, root)
    index = state.session_service.message_index
    prior = index._ensure("warm-legacy")
    assert prior is not None

    tree.append_message(
        Message(
            role="user",
            content=[
                MessageTextContentPart(text="private legacy verification"),
                MessageImageURLContentPart(
                    image_url=MessageImageURL(
                        url="data:image/png;base64,PRIVATE_LEGACY_IMAGE"
                    )
                ),
            ],
        )
    )
    private_id = tree.leaf_id
    assert private_id is not None
    tree.append_message(Message.assistant("verified"))
    _, exported = _save_tree(
        state,
        "warm-legacy",
        tree,
        root,
        existing_record=record,
    )
    refreshed = index._ensure("warm-legacy")
    assert refreshed is not None
    assert refreshed.indexed_size > prior.indexed_size
    assert refreshed.entries[private_id][1] == "tool_context"

    detail = client.get(
        f"/api/v1/session/warm-legacy/message/{private_id}",
        headers=_auth(),
    )
    assert detail.status_code == 404
    assert "private legacy verification" not in detail.text
    assert "PRIVATE_LEGACY_IMAGE" not in detail.text

    expected = [entry.id for entry in exported.entries if entry.id != private_id]
    if order == "desc":
        expected.reverse()
    seen, bodies = _paginate_messages(client, "warm-legacy", order=order)
    assert seen == expected
    assert len(seen) == len(set(seen))
    assert "private legacy verification" not in bodies
    assert "PRIVATE_LEGACY_IMAGE" not in bodies


def test_rejected_multifield_patches_have_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={
            "id": "patch-session",
            "location": {"directory": str(root)},
            "title": "original title",
        },
    )
    assert created.status_code == 200
    state = _state(client)
    store = state.session_service.store
    journal = state.event_journal
    cancelled: list[str] = []
    monkeypatch.setattr(
        state.runtime_registry,
        "cancel_automatic_title",
        cancelled.append,
    )

    def assert_unchanged(after: int) -> None:
        record = store.summary_record("patch-session")
        assert record is not None
        assert record.title == "original title"
        assert record.pinned is False
        assert record.archived_at is None
        events, _ = journal.history("patch-session", after=after, limit=20)
        assert not any(event.type == "session.updated" for event in events)

    before_invalid = journal.latest_sequence("patch-session")
    invalid = client.patch(
        "/api/v1/session/patch-session",
        headers=_auth(),
        json={"title": "rejected title", "pinned": None, "archived": False},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_pinned"
    assert_unchanged(before_invalid)
    assert cancelled == []

    write_prompt_queue_snapshot(
        store.directory,
        "patch-session",
        PersistedPromptQueue(
            revision=1,
            prompts=[
                PersistedPendingInput(
                    id="pending-1",
                    prompt="later",
                    kind="queued",
                    created_at="2026-09-05T00:00:00+00:00",
                )
            ],
        ),
    )
    before_pending = journal.latest_sequence("patch-session")
    pending = client.patch(
        "/api/v1/session/patch-session",
        headers=_auth(),
        json={"title": "also rejected", "pinned": True, "archived": True},
    )
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "session_has_pending_work"
    assert_unchanged(before_pending)
    assert cancelled == []
