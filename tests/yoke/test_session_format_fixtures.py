from __future__ import annotations

# ruff: noqa: D100,D103,S101

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil

import pytest

from yoke.session import SessionStore
from yoke.session.legacy.importer import import_legacy_sessions
from yoke.session.legacy.loader import load_legacy_session
from yoke.session.repository import SessionTreeIndex
from yoke.session.repository import utils

FIXTURES = Path(__file__).parent / "fixtures" / "session_format"
STAGES = ("loaded", "patched", "navigated", "forked")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 15, 12, tzinfo=UTC)


def _legacy_dir(tmp_path: Path, name: str) -> Path:
    """Lay out the committed JSONL files as a legacy session directory."""
    source = tmp_path / "legacy"
    source.mkdir()
    shutil.copyfile(FIXTURES / f"{name}.input.jsonl", source / f"{name}.jsonl")
    for stage in STAGES:
        session_id = f"{name}-fork" if stage == "forked" else f"{name}-{stage}"
        shutil.copyfile(
            FIXTURES / f"{name}.{stage}.jsonl", source / f"{session_id}.jsonl"
        )
    return source


@pytest.mark.parametrize("name", ["canonical", "legacy"])
def test_import_committed_session_formats(name: str, tmp_path: Path) -> None:
    """Every JSONL file the 6ad9e18 store wrote imports to its frozen record."""
    expected = json.loads((FIXTURES / f"{name}.records.json").read_text())
    source = _legacy_dir(tmp_path, name)
    store = SessionStore(tmp_path / "db")

    results = {r.session_id: r for r in import_legacy_sessions(store, source)}
    assert {r.status for r in results.values()} == {"imported"}

    assert store.load(name).model_dump(mode="json") == expected["loaded"]
    # Files written after each operation hold the same record under their own id.
    for stage in ("loaded", "patched", "navigated"):
        record = store.load(f"{name}-{stage}")
        assert record.model_dump(mode="json") == {
            **expected[stage],
            "id": f"{name}-{stage}",
        }
    fork = store.load(f"{name}-fork")
    assert fork.model_dump(mode="json") == expected["forked"]
    # The importer stores exactly what the JSONL loader returns.
    for session_id in results:
        assert (
            store.load(session_id).model_dump_json()
            == load_legacy_session(source, session_id).model_dump_json()
        )


@pytest.mark.parametrize("name", ["canonical", "legacy"])
def test_imported_session_supports_the_frozen_operations(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operations on an imported session give the records 6ad9e18 produced."""
    monkeypatch.setattr(utils, "datetime", FrozenDatetime)
    expected = json.loads((FIXTURES / f"{name}.records.json").read_text())
    source = _legacy_dir(tmp_path, name)
    store = SessionStore(tmp_path / "db")
    [result] = import_legacy_sessions(store, source, [name])
    assert result.status == "imported"

    record = store.load(name)
    assert record.model_dump(mode="json") == expected["loaded"]
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    assert [entry.id for entry in tree.active_entries()] == ["user", "answer"]

    record = store.set_title(name, "Patched title", existing_record=record)
    record = store.set_pinned(name, True, existing_record=record)
    entry = tree.entry_ref("answer").model_copy(update={"metadata": {"starred": True}})
    record = store.save_entry_metadata(
        name, entry, existing_record=record, tree_index=tree
    )
    assert store.load(name).model_dump(mode="json") == expected["patched"]

    record = store.save_tree_delta(
        name, existing_record=record, tree_index=tree, leaf_id="alternate"
    )
    assert [entry.id for entry in tree.active_entries()] == ["user", "alternate"]
    assert store.load(name).model_dump(mode="json") == expected["navigated"]

    forked = store.fork(name, new_session_id_value=f"{name}-fork")
    assert forked.model_dump(mode="json") == expected["forked"]
    assert store.load(forked.id) == forked


def test_import_is_idempotent_and_skips_existing(tmp_path: Path) -> None:
    source = _legacy_dir(tmp_path, "canonical")
    store = SessionStore(tmp_path / "db")
    first = [r.status for r in import_legacy_sessions(store, source)]
    second = [r.status for r in import_legacy_sessions(store, source)]
    assert set(first) == {"imported"}
    assert set(second) == {"skipped"}


def test_import_reports_unreadable_sessions(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "broken.jsonl").write_text("not json\n", encoding="utf-8")
    store = SessionStore(tmp_path / "db")
    [result] = import_legacy_sessions(store, source)
    assert result.status == "failed"
    assert not store.exists("broken")
