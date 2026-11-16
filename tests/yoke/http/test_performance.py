from __future__ import annotations


# ruff: noqa: ANN001,D100,D103,S101
from pathlib import Path


from tests.yoke.http.performance_helpers import messages as _messages
from yoke.session.repository.store import SessionStore
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.services.session_service import SessionService


def test_context_and_tree_inspectors_are_bounded_and_index_backed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("large", _messages(800, payload=64), root=tmp_path)
    service = SessionService(store, message_index=SessionMessageIndex(store))
    assert service.message_index.ensure("large") is not None

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
