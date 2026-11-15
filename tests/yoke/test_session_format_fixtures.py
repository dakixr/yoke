from __future__ import annotations

# ruff: noqa: D100,D103,S101

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil

import pytest

from yoke.session import SessionStore
from yoke.session.repository import SessionTreeIndex
from yoke.session.repository import utils

FIXTURES = Path(__file__).parent / "fixtures" / "session_format"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 15, 12, tzinfo=UTC)


@pytest.mark.parametrize("name", ["canonical", "legacy"])
def test_committed_session_format(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeze bytes and records produced by 6ad9e18, including legacy migration."""
    monkeypatch.setattr(utils, "datetime", FrozenDatetime)
    store = SessionStore(tmp_path)
    shutil.copyfile(FIXTURES / f"{name}.input.jsonl", store.session_path(name))
    expected_records = json.loads((FIXTURES / f"{name}.records.json").read_text())

    record = store.load(name)
    assert record.model_dump(mode="json") == expected_records["loaded"]
    assert (
        store.session_path(name).read_bytes()
        == (FIXTURES / f"{name}.loaded.jsonl").read_bytes()
    )
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    assert [entry.id for entry in tree.active_entries()] == ["user", "answer"]

    record = store.set_title(name, "Patched title", existing_record=record)
    record = store.set_pinned(name, True, existing_record=record)
    entry = tree.entry_ref("answer").model_copy(update={"metadata": {"starred": True}})
    record = store.save_entry_metadata(
        name, entry, existing_record=record, tree_index=tree
    )
    assert store.load(name).model_dump(mode="json") == expected_records["patched"]
    assert (
        store.session_path(name).read_bytes()
        == (FIXTURES / f"{name}.patched.jsonl").read_bytes()
    )

    record = store.save_tree_delta(
        name, existing_record=record, tree_index=tree, leaf_id="alternate"
    )
    assert [entry.id for entry in tree.active_entries()] == ["user", "alternate"]
    assert store.load(name).model_dump(mode="json") == expected_records["navigated"]
    assert (
        store.session_path(name).read_bytes()
        == (FIXTURES / f"{name}.navigated.jsonl").read_bytes()
    )

    forked = store.fork(name, new_session_id_value=f"{name}-fork")
    assert forked.model_dump(mode="json") == expected_records["forked"]
    assert store.load(forked.id) == forked
    assert (
        store.session_path(forked.id).read_bytes()
        == (FIXTURES / f"{name}.forked.jsonl").read_bytes()
    )
