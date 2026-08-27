from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path
import time

from fastapi.testclient import TestClient

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.observability import ToolTraceStore
from yoke.cli.session.store import SessionStore
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.http.services.session_service import SessionService
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.http.services.skill_service import SkillService
from yoke.http.services.tool_trace_service import ToolTraceService
from tests.yoke.http.performance_helpers import TOKEN
from tests.yoke.http.performance_helpers import auth_headers as _auth
from tests.yoke.http.performance_helpers import messages as _messages
from tests.yoke.http.performance_helpers import projected_text as _projected_text


def test_open_session_endpoints_do_not_deserialize_conversation_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    store = app.state.session_service.store
    root = tmp_path / "repo"
    root.mkdir()
    store.save("large", _messages(500, payload=2048), root=root)

    def fail(*_args, **_kwargs):
        raise AssertionError("browser open path must not call SessionStore.load")

    monkeypatch.setattr(store, "load", fail)
    client = TestClient(app)
    paths = [
        "/api/v1/session/large",
        "/api/v1/session/large/message?limit=100&order=desc&branch=active",
        "/api/v1/session/large/queue",
        "/api/v1/session/large/permission",
        "/api/v1/session/large/question",
    ]
    for path in paths:
        response = client.get(path, headers=_auth())
        assert response.status_code == 200, response.text


def test_session_metadata_patches_do_not_deserialize_history_or_lose_entry_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    store = app.state.session_service.store
    store.save("large", _messages(500, payload=2048), root=tmp_path)
    original_count = store.index_entry("large").entry_count

    def fail(*_args, **_kwargs):
        raise AssertionError("metadata patches must not deserialize session history")

    monkeypatch.setattr(store, "load", fail)
    client = TestClient(app)
    for patch in (
        {"title": "Renamed"},
        {"pinned": True},
        {"archived": True},
        {"archived": False},
    ):
        response = client.patch(
            "/api/v1/session/large",
            headers=_auth(),
            json=patch,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["tree"]["entryCount"] == original_count
    assert store.index_entry("large").entry_count == original_count


def test_message_offset_index_survives_service_restart_without_source_rescan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("large", _messages(800, payload=1024), root=tmp_path)
    first = SessionService(store)
    page = first.messages("large", limit=100, order="desc", cursor=None)
    assert len(page.data) == 100
    sidecar = store.directory / "read-index" / "large.json"
    deadline = time.monotonic() + 2
    while not sidecar.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sidecar.exists()
    first.close()

    restarted = SessionService(store)

    def fail(*_args, **_kwargs):
        raise AssertionError("current sidecar should avoid a source topology rescan")

    monkeypatch.setattr(restarted.message_index, "_scan", fail)
    page = restarted.messages("large", limit=100, order="desc", cursor=None)
    assert len(page.data) == 100


def test_message_offset_index_catches_up_append_without_full_record_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    initial_messages = _messages(200, payload=1024)
    record = store.save("large", initial_messages, root=tmp_path)
    service = SessionService(store)
    first = service.messages("large", limit=10, order="desc", cursor=None)
    assert len(first.data) == 10

    appended = [*initial_messages, Message.user("new tail")]
    store.save("large", appended, root=tmp_path, existing_record=record)

    def fail(*_args, **_kwargs):
        raise AssertionError("append refresh must not deserialize the old session")

    monkeypatch.setattr(store, "load", fail)
    refreshed = service.messages("large", limit=10, order="desc", cursor=None)
    assert _projected_text(refreshed.data[0]) == "new tail"


def test_read_cache_replays_in_place_store_append_exactly_once(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    original = _messages(200, payload=1024)
    store.save("large", original, root=tmp_path)
    cache = SessionReadCache(store)
    before = cache.get("large")
    old_count = len(before.record.conversation_entries)

    appended = [*before.record.messages, Message.user("new tail")]
    store.save(
        "large",
        appended,
        root=tmp_path,
        existing_record=before.record,
    )
    after = cache.get("large")

    assert len(after.record.conversation_entries) == old_count + 1
    assert after.record.messages[-1].display_text_content() == "new tail"
    assert len(after.entries_by_id) == old_count + 1


def test_live_tool_polling_does_not_load_large_persisted_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("large", _messages(500, payload=2048), root=tmp_path)
    traces = ToolTraceStore()
    traces.record_start(
        {
            "tool_call_id": "call-1",
            "tool_name": "read",
            "tool_arguments": '{"path":"README.md"}',
            "turn_id": 7,
            "iteration": 1,
        }
    )
    traces.record_output_delta(
        {
            "tool_call_id": "call-1",
            "tool_name": "read",
            "turn_id": 7,
            "text": "hello",
        }
    )

    class Runtime:
        def latest_turn_id(self) -> int:
            return 7

        def tool_trace_store(self) -> ToolTraceStore:
            return traces

    class Registry:
        def get_if_loaded(self, session_id: str):  # noqa: ANN202
            assert session_id == "large"
            return Runtime()

    def fail(*_args, **_kwargs):
        raise AssertionError("live tool polling must not deserialize session history")

    monkeypatch.setattr(store, "load", fail)
    service = ToolTraceService(store, Registry())  # type: ignore[arg-type]
    page = service.list_calls(
        "large",
        status=None,
        turn_id=7,
        limit=100,
        cursor=None,
    )
    assert [item.id for item in page.data] == ["call-1"]
    assert service.call("large", "call-1").data.id == "call-1"
    output = service.output("large", "call-1", after_seq=0, limit=100)
    assert [chunk.text for chunk in output.data] == ["hello"]


def test_historical_tool_list_reads_only_tool_turns_from_topology_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    tool_call = ToolCall(
        id="persisted-call",
        function=ToolFunction(name="read", arguments='{"path":"README.md"}'),
    )
    messages = [
        *_messages(300, payload=2048),
        Message.user("inspect the file"),
        Message(role="assistant", tool_calls=[tool_call]),
        Message(role="tool", tool_call_id=tool_call.id, content='{"ok":true}'),
        Message.assistant("done"),
    ]
    store.save("large", messages, root=tmp_path)
    session_service = SessionService(store)
    assert session_service.message_index._ensure("large") is not None

    class Registry:
        def get_if_loaded(self, session_id: str):  # noqa: ANN202
            del session_id
            return None

    def fail(*_args, **_kwargs):
        raise AssertionError(
            "historical tool listing must not deserialize full history"
        )

    monkeypatch.setattr(store, "load", fail)
    service = ToolTraceService(
        store,
        Registry(),  # type: ignore[arg-type]
        message_index=session_service.message_index,
    )
    page = service.list_calls(
        "large",
        status=None,
        turn_id=None,
        limit=100,
        cursor=None,
    )

    assert [item.id for item in page.data] == [tool_call.id]
    assert page.data[0].tool_name == "read"
    assert page.data[0].status == "ok"


def test_session_skills_use_index_metadata_without_loading_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    skill = ActiveSkill(
        name="demo",
        activation_id="activation-1",
        description="Demo skill",
        source_path="<inline>",
        content="# Demo",
    )
    store.save(
        "large",
        _messages(300, payload=2048),
        root=tmp_path,
        active_skills=[skill],
        skill_dirs=["skills"],
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("skill inspection must not deserialize session history")

    monkeypatch.setattr(store, "load", fail)
    response = SkillService(store).session_skills("large")

    assert [item.name for item in response.data.active] == ["demo"]


def test_context_and_tree_inspectors_are_bounded_and_index_backed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("large", _messages(800, payload=2048), root=tmp_path)
    service = SessionService(store)
    assert service.message_index._ensure("large") is not None

    def fail(*_args, **_kwargs):
        raise AssertionError("bounded inspectors must not deserialize full history")

    monkeypatch.setattr(store, "load", fail)
    context = service.context(
        "large",
        include_system=False,
        include_tool_results=True,
        limit=50,
        max_chars=500_000,
    )
    tree = service.tree("large", limit=50, cursor=None)

    assert len(context.data.messages) == 50
    assert context.data.total_entries == 800
    assert context.data.retained_entries == 50
    assert context.data.truncated is True
    assert len(tree.data.entries) == 50
    assert tree.data.total_entries == 800
    assert tree.data.cursor.next is not None

    older = service.tree("large", limit=50, cursor=tree.data.cursor.next)
    assert len(older.data.entries) == 50
    assert older.data.entries[-1].id != tree.data.entries[-1].id
