"""Canonical tool-call windows through the real HTTP service and session store."""

from __future__ import annotations

# ruff: noqa: D102,D103,S101

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient
import pytest

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.observability import ToolTraceStore
from yoke.agent.session_tree import SessionTree
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.http.errors import ApiError
from yoke.http.models.tool_trace import ToolCallListResponse
from yoke.http.services.cursor import encode_cursor
from yoke.http.services.cursor import query_fingerprint
from yoke.http.services.tool_trace_service import ToolTraceService
from yoke.session import SessionStore


SESSION = "chronology"
Order = Literal["oldest", "latest"]


class _Runtime:
    def __init__(self, traces: ToolTraceStore) -> None:
        self.traces = traces

    def tool_trace_store(self) -> ToolTraceStore:
        return self.traces

    def latest_turn_id(self) -> int:
        return 2


class _Registry:
    def __init__(self, traces: ToolTraceStore | None) -> None:
        self.runtime = _Runtime(traces) if traces is not None else None

    def get_if_loaded(self, session_id: str) -> _Runtime | None:
        assert session_id == SESSION
        return self.runtime


def _ids(count: int) -> list[str]:
    # Deliberately opposite lexical order. IDs do not encode chronology.
    return [f"opaque-{count - index:04d}" for index in range(count)]


def _messages(ids: list[str]) -> list[Message]:
    messages = [Message.user("inspect these calls")]
    # Multiple calls per assistant message also have a canonical order.
    for start in range(0, len(ids), 3):
        batch = ids[start : start + 3]
        messages.append(
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        function=ToolFunction(name="read", arguments="{}"),
                    )
                    for call_id in batch
                ],
            )
        )
        messages.extend(
            Message(role="tool", tool_call_id=call_id, content='{"ok":true}')
            for call_id in batch
        )
    return messages


def _save(store: SessionStore, ids: list[str], root: Path) -> None:
    messages = _messages(ids)
    exported = SessionTree.from_messages(messages).export_for_persistence()
    store.save(
        SESSION,
        messages,
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
    )


def _service(
    tmp_path: Path, ids: list[str], traces: ToolTraceStore | None = None
) -> ToolTraceService:
    store = SessionStore(tmp_path / "sessions")
    _save(store, ids, tmp_path)
    return ToolTraceService(store, _Registry(traces))


def _page(
    service: ToolTraceService,
    *,
    order: Order = "oldest",
    limit: int = 100,
    cursor: str | None = None,
    status: str | None = None,
    turn_id: int | None = None,
) -> ToolCallListResponse:
    return service.list_calls(
        SESSION, order=order, limit=limit, cursor=cursor, status=status, turn_id=turn_id
    )


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101, 237])
@pytest.mark.parametrize("order", ["oldest", "latest"])
def test_windows_cover_every_saved_call_once(
    tmp_path: Path, count: int, order: Order
) -> None:
    ids = _ids(count)
    service = _service(tmp_path, ids)
    pages: list[list[str]] = []
    cursor = None
    while True:
        page = _page(service, order=order, cursor=cursor)
        assert page.total == count
        assert page.cursor.previous is None
        assert len(page.data) <= 100
        sequences = [item.sequence for item in page.data if item.sequence is not None]
        assert len(sequences) == len(page.data)
        assert sequences == sorted(sequences)
        for item in page.data:
            assert item.sequence == ids.index(item.id) + 1
            assert item.time.started is None
            assert item.time.ended is None
            assert item.time.duration_ms is None
        pages.append([item.id for item in page.data])
        cursor = page.cursor.next
        if cursor is None:
            break
        assert len(pages) <= 3, "pagination did not terminate"
    assert pages[0] == (ids[-100:] if order == "latest" else ids[:100])
    chronological_pages = reversed(pages) if order == "latest" else iter(pages)
    flattened = [call_id for page_ids in chronological_pages for call_id in page_ids]
    assert flattened == ids
    assert len(set(flattened)) == count
    if ids:
        assert service.call(SESSION, ids[-1]).data.sequence == count


def test_default_and_preexisting_cursors_remain_oldest_first(tmp_path: Path) -> None:
    ids = _ids(237)
    service = _service(tmp_path, ids)
    default = service.list_calls(
        SESSION, status=None, turn_id=None, limit=100, cursor=None
    )
    assert default == _page(service, order="oldest")
    assert [item.id for item in default.data] == ids[:100]
    legacy_cursor = encode_cursor(
        query=query_fingerprint((SESSION, None, None)), anchor_id=ids[99]
    )
    assert default.cursor.next == legacy_cursor
    assert [item.id for item in _page(service, cursor=legacy_cursor).data] == ids[
        100:200
    ]


@pytest.mark.parametrize("second_time", ["2025-01-02", "2025-01-01"])
def test_live_overlay_keeps_transcript_order_with_missing_equal_or_reversed_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_time: str
) -> None:
    now = datetime(2025, 1, 2, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(_tz: object) -> datetime:
            return now

    monkeypatch.setattr("yoke.agent.observability.tool_trace.datetime", Clock)
    ids = _ids(140)
    traces = ToolTraceStore()
    # Live start order disagrees with transcript order for overlapping calls.
    for call_id in [ids[130], ids[6], "live-z", "live-a"]:
        traces.record_start({"tool_call_id": call_id, "turn_id": 2})
        now = datetime.fromisoformat(second_time).replace(tzinfo=UTC)
    service = _service(tmp_path, ids, traces)
    expected = [*ids, "live-z", "live-a"]
    page = _page(service, order="latest")
    assert [item.id for item in page.data] == expected[-100:]
    assert [item.sequence for item in page.data] == list(range(43, 143))
    assert page.total == 142
    earlier = _page(service, order="latest", cursor=page.cursor.next)
    assert [item.id for item in earlier.data] == expected[:42]
    assert earlier.cursor.next is None
    for index, call_id in enumerate(expected, start=1):
        detail = service.call(SESSION, call_id).data
        is_live = call_id in {ids[130], ids[6], "live-z", "live-a"}
        assert detail.sequence == (None if is_live else index)
        assert detail.retention == ("runtime" if is_live else "session")
        assert (detail.time.started is not None) == is_live
    current_turn = _page(service, turn_id=2, order="latest")
    assert [item.id for item in current_turn.data] == [
        ids[6],
        ids[130],
        "live-z",
        "live-a",
    ]
    assert [item.sequence for item in current_turn.data] == [7, 131, 141, 142]


@pytest.mark.parametrize("order", ["oldest", "latest"])
@pytest.mark.parametrize(
    "status", [None, "pending", "running", "ok", "failed", "cancelled"]
)
@pytest.mark.parametrize("turn_id", [None, 1, 2, 3])
def test_filters_keep_global_sequences_and_page_the_matching_total(
    tmp_path: Path, order: Order, status: str | None, turn_id: int | None
) -> None:
    ids = _ids(14)
    traces = ToolTraceStore()
    live_ids = [*ids[-4:], "live-z", "live-a", "live-m"]
    expected: list[tuple[str, str, int | None]] = [
        (call_id, "ok", None) for call_id in ids[:-4]
    ]
    for index, call_id in enumerate(live_ids):
        turn = index % 2 + 1
        state = "running" if index % 3 == 0 else "ok" if index % 3 == 1 else "failed"
        traces.record_start({"tool_call_id": call_id, "turn_id": turn})
        if state != "running":
            traces.record_end(
                {"tool_call_id": call_id, "turn_id": turn, "ok": state == "ok"}
            )
        expected.append((call_id, state, turn))
    traces.record_start({"tool_call_id": "cancelled", "turn_id": 3})
    traces.retire_turn(3)
    expected.append(("cancelled", "cancelled", 3))
    service = _service(tmp_path, ids, traces)
    matches = [
        (call_id, None if order == "oldest" and turn_id == 2 else sequence)
        for sequence, (call_id, state, turn) in enumerate(expected, start=1)
        if (status is None or state == status) and (turn_id is None or turn == turn_id)
    ]
    pages: list[list[tuple[str, int | None]]] = []
    cursor = None
    while True:
        page = _page(
            service, order=order, status=status, turn_id=turn_id, limit=2, cursor=cursor
        )
        assert page.total == len(matches)
        pages.append([(item.id, item.sequence) for item in page.data])
        cursor = page.cursor.next
        if cursor is None:
            break
        assert len(pages) <= len(matches)
    chronological_pages = reversed(pages) if order == "latest" else iter(pages)
    assert [
        item for page_items in chronological_pages for item in page_items
    ] == matches


@pytest.mark.parametrize("order", ["oldest", "latest"])
@pytest.mark.parametrize("saved_count", [0, 137, 237])
def test_cursor_survives_persisted_and_live_appends(
    tmp_path: Path, order: Order, saved_count: int
) -> None:
    ids = _ids(237)
    traces = ToolTraceStore()
    for call_id in [*ids[saved_count:], "live-z"]:
        traces.record_start({"tool_call_id": call_id, "turn_id": 2})
    service = _service(tmp_path, ids[:saved_count], traces)
    original = [*ids, "live-z"]
    first = _page(service, order=order)
    assert first.total == 238
    # Persist formerly live-only calls, then append saved and live history.
    saved = [*original, "new-saved"]
    _save(service.store, saved, tmp_path)
    traces.record_start({"tool_call_id": "new-live", "turn_id": 2})
    pages = [[item.id for item in first.data]]
    cursor = first.cursor.next
    while cursor is not None:
        page = _page(service, order=order, cursor=cursor, limit=37)
        assert page.total == 240
        pages.append([item.id for item in page.data])
        cursor = page.cursor.next
        assert len(pages) < 10
    chronological_pages = reversed(pages) if order == "latest" else iter(pages)
    flattened = [call_id for page_ids in chronological_pages for call_id in page_ids]
    assert flattened == (original if order == "latest" else [*saved, "new-live"])
    assert len(set(flattened)) == len(flattened)
    refreshed = _page(service, order="latest")
    assert [item.id for item in refreshed.data] == [*saved, "new-live"][-100:]
    assert refreshed.data[-1].sequence == 240


@pytest.mark.parametrize("order", ["oldest", "latest"])
def test_invalid_and_mismatched_cursors(tmp_path: Path, order: Order) -> None:
    ids = _ids(4)
    service = _service(tmp_path, ids)
    cursor = _page(service, order=order, limit=1).cursor.next
    assert cursor is not None
    mismatches: list[tuple[Order, str | None, int | None]] = [
        ("latest" if order == "oldest" else "oldest", None, None),
        (order, "ok", None),
        (order, None, 2),
    ]
    for other_order, status, turn_id in mismatches:
        with pytest.raises(ApiError) as caught:
            _page(
                service,
                order=other_order,
                status=status,
                turn_id=turn_id,
                cursor=cursor,
            )
        assert caught.value.status_code == 400
        assert caught.value.code == "cursor_query_mismatch"
    for invalid in ["!", "null", "W10", "bnVsbA", "e30"]:
        with pytest.raises(ApiError) as caught:
            _page(service, order=order, cursor=invalid)
        assert caught.value.code == "invalid_cursor"
    parts = (SESSION, None, None, order) if order == "latest" else (SESSION, None, None)
    foreign_cursor = encode_cursor(
        query=query_fingerprint(("other-session", *parts[1:])), anchor_id=ids[0]
    )
    with pytest.raises(ApiError) as caught:
        _page(service, order=order, cursor=foreign_cursor)
    assert caught.value.code == "cursor_query_mismatch"
    for invalid in [
        encode_cursor(query=query_fingerprint(parts), anchor_id="missing"),
        cursor,
    ]:
        # A real previously issued cursor also fails after its anchor disappears.
        _save(service.store, [], tmp_path)
        with pytest.raises(ApiError) as caught:
            _page(service, order=order, cursor=invalid)
        assert caught.value.code == "invalid_cursor_anchor"


def test_http_order_default_latest_and_validation(tmp_path: Path) -> None:
    app = create_app(
        HttpAppSettings(
            auth_token="test-token", session_directory=tmp_path / "sessions"
        )
    )
    ids = _ids(237)
    _save(app.state.tool_trace_service.store, ids, tmp_path)
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    url = f"/api/v1/session/{SESSION}/tool-call"
    oldest = client.get(url)
    assert oldest.status_code == 200
    assert [item["id"] for item in oldest.json()["data"]] == ids[:100]
    latest = client.get(url, params={"order": "latest", "limit": 100})
    assert latest.status_code == 200
    payload = latest.json()
    assert payload["total"] == 237
    assert [item["id"] for item in payload["data"]] == ids[-100:]
    assert [item["sequence"] for item in payload["data"]] == list(range(138, 238))
    previous = client.get(
        url, params={"order": "latest", "cursor": payload["cursor"]["next"]}
    )
    assert previous.status_code == 200
    assert [item["id"] for item in previous.json()["data"]] == ids[37:137]
    detail = client.get(f"{url}/{ids[-1]}")
    assert detail.status_code == 200
    assert detail.json()["data"]["sequence"] == 237
    assert detail.json()["data"]["time"]["started"] is None
    mismatch = client.get(url, params={"cursor": payload["cursor"]["next"]})
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "cursor_query_mismatch"
    for params in [{"order": "newest"}, {"limit": 0}, {"limit": 201}]:
        assert client.get(url, params=params).status_code == 422
    assert client.get(url, params={"cursor": "!"}).status_code == 400
    assert client.get(f"{url}/missing").status_code == 404
    assert client.get("/api/v1/session/missing/tool-call").status_code == 404


def test_detail_reuses_persisted_provider_result_projection(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    tool_call = ToolCall(
        id="command-call",
        function=ToolFunction(name="exec_command", arguments='{"cmd":"printf done"}'),
    )
    canonical = {
        "ok": True,
        "exit_code": 0,
        "returncode": 0,
        "running": False,
        "wall_time_seconds": 0.02,
        "output": "done",
        "outputTruncationDetails": {"truncated": False},
    }
    tool_result = Message.tool(
        tool_call.id,
        json.dumps(canonical, separators=(",", ":")),
    )
    tool_result._provider_result_projection = "command"
    messages = [
        Message.user("run it"),
        Message(role="assistant", tool_calls=[tool_call]),
        tool_result,
    ]
    exported = SessionTree.from_messages(messages).export_for_persistence()
    store.save(
        SESSION,
        messages,
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=tmp_path,
    )
    service = ToolTraceService(store, _Registry(None))

    detail = service.call(SESSION, tool_call.id).data

    assert detail.result == canonical
    assert detail.result_projection == '{"ok":true,"output":"done"}'
    assert _page(service).data[0].result_projection is None


def test_detail_reuses_live_provider_result_projection(tmp_path: Path) -> None:
    traces = ToolTraceStore()
    traces.record_start(
        {
            "tool_call_id": "live-command",
            "tool_name": "exec_command",
            "tool_arguments": '{"cmd":"printf done"}',
            "turn_id": 2,
        }
    )
    traces.record_end(
        {
            "tool_call_id": "live-command",
            "tool_name": "exec_command",
            "turn_id": 2,
            "ok": True,
            "result": {
                "ok": True,
                "exit_code": 0,
                "returncode": 0,
                "running": False,
                "output": "done",
                "outputTruncationDetails": {"truncated": False},
            },
            "provider_result_projection": "command",
        }
    )
    service = _service(tmp_path, [], traces)

    detail = service.call(SESSION, "live-command").data

    assert detail.result_projection == '{"ok":true,"output":"done"}'


def test_detail_does_not_infer_projection_from_tool_name(tmp_path: Path) -> None:
    traces = ToolTraceStore()
    traces.record_end(
        {
            "tool_call_id": "hooked-command",
            "tool_name": "exec_command",
            "ok": True,
            "result": {
                "ok": True,
                "returncode": 0,
                "output": "hook changed this result",
                "outputTruncationDetails": {"truncated": False},
            },
        }
    )
    service = _service(tmp_path, [], traces)

    assert service.call(SESSION, "hooked-command").data.result_projection is None


def test_result_projection_is_built_after_public_redaction(tmp_path: Path) -> None:
    traces = ToolTraceStore()
    traces.record_end(
        {
            "tool_call_id": "redacted-command",
            "tool_name": "exec_command",
            "ok": True,
            "result": {
                "ok": True,
                "returncode": 0,
                "output": "done",
                "outputTruncationDetails": {"truncated": False},
                "password": "do-not-leak",
            },
            "provider_result_projection": "command",
        }
    )
    service = _service(tmp_path, [], traces)

    detail = service.call(SESSION, "redacted-command").data

    assert detail.result is not None
    assert detail.result["password"] == "<redacted>"
    assert detail.result_projection is not None
    assert "do-not-leak" not in detail.result_projection
    assert json.loads(detail.result_projection)["password"] == "<redacted>"


def test_live_polling_never_reconstructs_saved_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces = ToolTraceStore()
    traces.record_start({"tool_call_id": "live", "turn_id": 2})
    service = _service(tmp_path, _ids(237), traces)

    def unexpected_history(*_args: object) -> None:
        raise AssertionError("Live polling must not reconstruct saved payloads")

    monkeypatch.setattr(service, "_persisted_trace_snapshot", unexpected_history)
    page = _page(service, turn_id=2)
    assert [item.id for item in page.data] == ["live"]
    assert page.data[0].sequence is None
    assert service.call(SESSION, "live").data.id == "live"
