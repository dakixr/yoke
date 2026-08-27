from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path
import time

from fastapi.testclient import TestClient

from yoke.agent.loop import AgentResult
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import SessionTree
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.store import SessionStore
from yoke.cli.session.writer import append_session_metadata
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.http.services.session_service import SessionService
from yoke.session.events import SessionEventJournal
from tests.yoke.http.performance_helpers import TOKEN
from tests.yoke.http.performance_helpers import auth_headers as _auth
from tests.yoke.http.performance_helpers import messages as _messages
from tests.yoke.http.performance_helpers import projected_text as _projected_text


def test_live_turn_reuses_shared_session_snapshot_after_initial_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Agent:
        supports_user_message = True

        def __init__(self, record: SessionRecord) -> None:
            self.record = record

        def run(
            self,
            prompt: str,
            *,
            user_message: Message | None = None,
            on_event=None,
            stop_requested=None,
        ) -> AgentResult:
            del stop_requested
            if on_event is not None:
                on_event(
                    "tool_execution_start",
                    {
                        "iteration": 1,
                        "tool_name": "read",
                        "tool_call_id": "call-fast",
                        "tool_arguments": "{}",
                    },
                )
                on_event(
                    "tool_execution_end",
                    {
                        "iteration": 1,
                        "tool_name": "read",
                        "tool_call_id": "call-fast",
                        "ok": True,
                        "result": {"ok": True},
                    },
                )
            tree = SessionTree.restore(
                self.record.conversation_entries,
                self.record.leaf_id,
            )
            tree.append_message(user_message or Message.user(prompt))
            tree.append_message(Message.assistant("done"))
            view = tree.project(ConversationProjection())
            if on_event is not None:
                on_event(
                    "assistant_message",
                    {"phase": "final_answer", "content": "done"},
                )
            return AgentResult(
                output="done",
                messages=list(view.transcript_messages),
                iterations=1,
                conversation_entries=list(tree.entries),
            )

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=Agent,
        )
    )
    store = app.state.session_service.store
    store.save("large", _messages(500, payload=2048), root=tmp_path)
    app.state.session_service.read_cache.get("large")

    def fail(*_args, **_kwargs):
        raise AssertionError("live turn must reuse the primed shared read snapshot")

    monkeypatch.setattr(store, "load", fail)
    with TestClient(app) as client:
        admitted = client.post(
            "/api/v1/session/large/prompt",
            headers=_auth(),
            json={
                "id": "input-fast",
                "prompt": {"text": "go"},
                "delivery": "steer",
                "resume": True,
            },
        )
        assert admitted.status_code == 200, admitted.text
        for _ in range(200):
            active = client.get("/api/v1/session/active", headers=_auth())
            if "large" not in active.json()["data"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("fixture turn did not settle")
        messages = client.get(
            "/api/v1/session/large/message?limit=10&order=desc&branch=active",
            headers=_auth(),
        )
        assert messages.status_code == 200
        assert any(
            part.get("text") == "done"
            for message in messages.json()["data"]
            for part in message.get("content", [])
            if part.get("type") == "text"
        )


def test_descending_cold_message_page_uses_tail_without_topology_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("large", _messages(800, payload=2048), root=tmp_path)
    service = SessionService(store)
    monkeypatch.setattr(service.message_index, "warm_async", lambda _session_id: None)

    def fail(*_args, **_kwargs):
        raise AssertionError("cold latest page must not build the full topology index")

    monkeypatch.setattr(service.message_index, "_scan", fail)
    monkeypatch.setattr(store, "load", fail)
    page = service.messages("large", limit=100, order="desc", cursor=None)

    assert len(page.data) == 100
    assert _projected_text(page.data[0]).startswith("00799 ")
    assert _projected_text(page.data[-1]).startswith("00700 ")


def test_descending_tail_page_follows_selected_branch(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    root = ConversationEntry(id="root", kind="user", message=Message.user("root"))
    shared = ConversationEntry(
        id="shared",
        kind="assistant",
        message=Message.assistant("shared"),
        parent_id=root.id,
    )
    main_user = ConversationEntry(
        id="main-user",
        kind="user",
        message=Message.user("main user"),
        parent_id=shared.id,
    )
    main_assistant = ConversationEntry(
        id="main-assistant",
        kind="assistant",
        message=Message.assistant("main assistant"),
        parent_id=main_user.id,
    )
    branch_user = ConversationEntry(
        id="branch-user",
        kind="user",
        message=Message.user("branch user"),
        parent_id=shared.id,
    )
    branch_assistant = ConversationEntry(
        id="branch-assistant",
        kind="assistant",
        message=Message.assistant("branch assistant"),
        parent_id=branch_user.id,
    )
    record = SessionRecord(
        id="branch",
        root=str(tmp_path),
        leaf_id=branch_assistant.id,
        conversation_entries=[
            root,
            shared,
            main_user,
            main_assistant,
            branch_user,
            branch_assistant,
        ],
    )
    store._write_session_record(record)
    store._update_index(record)
    service = SessionService(store)
    monkeypatch.setattr(service.message_index, "warm_async", lambda _session_id: None)

    page = service.messages("branch", limit=20, order="desc", cursor=None)
    texts = [_projected_text(item) for item in page.data]

    assert texts == ["branch assistant", "branch user", "shared", "root"]


def test_descending_tail_page_honors_metadata_only_leaf_with_stale_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    root = ConversationEntry(id="root", kind="user", message=Message.user("root"))
    middle = ConversationEntry(
        id="middle",
        kind="assistant",
        message=Message.assistant("middle"),
        parent_id=root.id,
    )
    tail = ConversationEntry(
        id="tail",
        kind="user",
        message=Message.user("tail"),
        parent_id=middle.id,
    )
    record = SessionRecord(
        id="checkout",
        root=str(tmp_path),
        leaf_id=tail.id,
        conversation_entries=[root, middle, tail],
    )
    store._write_session_record(record)
    store._update_index(record)
    append_session_metadata(store._session_path("checkout"), {"leaf_id": middle.id})
    service = SessionService(store)
    monkeypatch.setattr(service.message_index, "warm_async", lambda _session_id: None)

    page = service.messages("checkout", limit=20, order="desc", cursor=None)
    texts = [_projected_text(item) for item in page.data]

    assert texts == ["middle", "root"]


def test_history_pages_stream_instead_of_reading_complete_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = SessionEventJournal(tmp_path)
    for index in range(1000):
        journal.append("session", "test.event", {"index": index})

    original_read_text = Path.read_text

    def fail_read_text(self: Path, *args, **kwargs):
        if self.name == "session.jsonl" and self.parent.name == "events":
            raise AssertionError("history must not read the complete journal")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    first, first_more = journal.history("session", after=0, limit=200)
    second, second_more = journal.history("session", after=200, limit=200)
    assert [item.seq for item in first] == list(range(1, 201))
    assert [item.seq for item in second] == list(range(201, 401))
    assert first_more is True
    assert second_more is True


def test_create_app_does_not_run_session_maintenance_before_server_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("create_app must not block on session maintenance")

    monkeypatch.setattr(SessionStore, "maintain_index", fail)
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    assert (
        app.state.session_service.store.directory == (tmp_path / "sessions").resolve()
    )


def test_message_snapshot_sequence_skips_pre_snapshot_history_replay(
    tmp_path: Path,
) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    store = app.state.session_service.store
    store.save("session", [Message.user("hello")], root=tmp_path)
    journal = app.state.event_journal
    for index in range(17):
        journal.append("session", "test.event", {"index": index})

    response = TestClient(app).get(
        "/api/v1/session/session/message?limit=100&order=desc&branch=active",
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["snapshotSeq"] == 17
