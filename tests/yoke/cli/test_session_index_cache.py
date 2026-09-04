from __future__ import annotations

# ruff: noqa: D100,D103,S101

import multiprocessing
from pathlib import Path
import re
import time
from typing import Any

import pytest

from yoke.agent.models import Message
from yoke.cli.session import SessionStore
from yoke.cli.session.index_cache import INDEX_WRITE_ATTEMPTS
from yoke.cli.session.index_cache import INDEX_WRITE_RETRY_SECONDS
from yoke.cli.session.index_cache import SessionIndexCache
from yoke.cli.session.maintenance import prune_index_and_sessions
from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry


def _update_index_in_process(
    path: str,
    session_id: str,
    ready: Any,
    entered: Any,
    release: Any | None,
) -> None:
    cache = SessionIndexCache(Path(path))

    def mutate(index: SessionIndex) -> bool:
        index.sessions[session_id] = SessionIndexEntry(id=session_id)
        entered.set()
        if release is not None and not release.wait(timeout=5):
            raise TimeoutError("Timed out waiting to release index transaction.")
        return True

    ready.set()
    cache.update(mutate)


def _prune_with_paused_session_scan(
    directory: str,
    scan_complete: Any,
    release_scan: Any,
) -> None:
    import yoke.cli.session.store as store_module

    original_session_file_ids = store_module.session_file_ids

    def paused_session_file_ids(
        directory: Path,
        *,
        session_file_suffix: str,
        session_id_pattern: re.Pattern[str],
    ) -> set[str] | None:
        session_ids = original_session_file_ids(
            directory,
            session_file_suffix=session_file_suffix,
            session_id_pattern=session_id_pattern,
        )
        scan_complete.set()
        if not release_scan.wait(timeout=10):
            raise TimeoutError("Timed out waiting to release the prune scan.")
        return session_ids

    setattr(store_module, "session_file_ids", paused_session_file_ids)
    SessionStore(Path(directory))._prune_index_and_sessions()


def _save_session_in_process(directory: str, session_id: str) -> None:
    SessionStore(Path(directory)).save(
        session_id,
        [Message.user("created while maintenance held the index lock")],
        root=Path(directory).parent,
    )


def test_index_write_retries_permission_errors_and_uses_unique_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.index_cache as index_cache_module

    cache = SessionIndexCache(tmp_path / "index.json")
    attempts: list[Path] = []
    sleeps: list[float] = []
    original_replace = cache._replace_once

    def flaky_replace(temporary: Path) -> None:
        attempts.append(temporary)
        if len(attempts) < 3:
            raise PermissionError("index is temporarily locked")
        original_replace(temporary)

    monkeypatch.setattr(cache, "_replace_once", flaky_replace)
    monkeypatch.setattr(index_cache_module.time, "sleep", sleeps.append)

    cache.write(SessionIndex(sessions={"first": SessionIndexEntry(id="first")}))
    first_temporary = attempts[0]
    assert attempts[:3] == [first_temporary, first_temporary, first_temporary]
    assert sleeps == [INDEX_WRITE_RETRY_SECONDS, INDEX_WRITE_RETRY_SECONDS * 2]
    assert not first_temporary.exists()

    cache.write(SessionIndex(sessions={"second": SessionIndexEntry(id="second")}))
    assert attempts[-1] != first_temporary
    assert set(cache.read().sessions) == {"second"}


def test_index_updates_coordinate_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    index_path = tmp_path / "index.json"
    first_ready = context.Event()
    first_entered = context.Event()
    second_ready = context.Event()
    second_entered = context.Event()
    release_first = context.Event()
    first = context.Process(
        target=_update_index_in_process,
        args=(
            str(index_path),
            "first",
            first_ready,
            first_entered,
            release_first,
        ),
    )
    second = context.Process(
        target=_update_index_in_process,
        args=(str(index_path), "second", second_ready, second_entered, None),
    )
    try:
        first.start()
        assert first_ready.wait(timeout=5)
        assert first_entered.wait(timeout=5)
        second.start()
        assert second_ready.wait(timeout=5)
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)
    finally:
        release_first.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert set(SessionIndexCache(index_path).read().sessions) == {"first", "second"}


def test_index_update_reads_disk_after_another_cache_writes(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    first = SessionIndexCache(index_path)
    second = SessionIndexCache(index_path)
    first.write(SessionIndex(sessions={"base": SessionIndexEntry(id="base")}))
    assert set(second.read().sessions) == {"base"}

    first.update(lambda index: _add_index_entry(index, SessionIndexEntry(id="first")))
    second.update(lambda index: _add_index_entry(index, SessionIndexEntry(id="second")))

    assert set(SessionIndexCache(index_path).read().sessions) == {
        "base",
        "first",
        "second",
    }


def test_index_update_reuses_unchanged_cached_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SessionIndexCache(tmp_path / "index.json")
    cache.write(SessionIndex(sessions={"base": SessionIndexEntry(id="base")}))

    def fail_read() -> SessionIndex:
        raise AssertionError("unchanged cached index should not be parsed again")

    monkeypatch.setattr(cache, "_read_disk", fail_read)

    cache.update(lambda index: _add_index_entry(index, SessionIndexEntry(id="next")))

    assert set(cache.read().sessions) == {"base", "next"}


def test_maintenance_preserves_index_when_directory_scan_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.index as index_module

    store = SessionStore(tmp_path / "sessions")
    store.save("kept", [], root=tmp_path)

    def fail_scan(_directory: Path):
        raise PermissionError("scan denied")

    monkeypatch.setattr(index_module.os, "scandir", fail_scan)

    store.maintain_index(force=True)

    assert store.index_entry("kept") is not None


def test_maintenance_retries_summary_after_one_entry_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.index as index_module

    store = SessionStore(tmp_path / "sessions")
    store.save("kept", [Message.user("still here")], root=tmp_path)
    original_scandir = index_module.os.scandir
    stat_failed = False

    class EntryProxy:
        def __init__(self, entry) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def stat(self):
            nonlocal stat_failed
            if self.name == "kept.jsonl" and not stat_failed:
                stat_failed = True
                raise PermissionError("transient metadata failure")
            return self._entry.stat()

    class ScandirProxy:
        def __init__(self, path: Path) -> None:
            self._scan = original_scandir(path)

        def __enter__(self):
            self._entries = self._scan.__enter__()
            return self

        def __iter__(self):
            return (EntryProxy(entry) for entry in self._entries)

        def __exit__(self, exc_type, exc, traceback):
            return self._scan.__exit__(exc_type, exc, traceback)

    monkeypatch.setattr(index_module.os, "scandir", ScandirProxy)

    store.maintain_index(force=True)

    assert stat_failed is True
    assert store.index_entry("kept") is not None
    assert store.load("kept").messages[-1].text_content() == "still here"


def test_maintenance_removes_file_deleted_between_repair_and_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.store as store_module

    store = SessionStore(tmp_path / "sessions")
    store.save("deleted-during-maintenance", [], root=tmp_path)
    session_path = store._session_path("deleted-during-maintenance")

    def delete_before_prune(*_args, **_kwargs) -> set[str]:
        session_path.unlink()
        return set()

    monkeypatch.setattr(store_module, "session_file_ids", delete_before_prune)

    store.maintain_index(force=True)

    assert store.index_entry("deleted-during-maintenance") is None


def test_prune_scans_session_names_inside_the_index_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.store as store_module

    store = SessionStore(tmp_path / "sessions")
    store.save("kept", [], root=tmp_path)
    original_update = store._index_cache.update
    original_session_file_ids = store_module.session_file_ids
    inside_transaction = False
    scan_observed = False

    def tracked_update(mutator):
        def tracked_mutator(index):
            nonlocal inside_transaction
            inside_transaction = True
            try:
                return mutator(index)
            finally:
                inside_transaction = False

        return original_update(tracked_mutator)

    def tracked_session_file_ids(*args, **kwargs):
        nonlocal scan_observed
        assert inside_transaction is True
        scan_observed = True
        return original_session_file_ids(*args, **kwargs)

    monkeypatch.setattr(store._index_cache, "update", tracked_update)
    monkeypatch.setattr(store_module, "session_file_ids", tracked_session_file_ids)

    store._prune_index_and_sessions()

    assert scan_observed is True
    assert store.index_entry("kept") is not None


def test_concurrent_session_creation_survives_a_stale_prune_scan(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    directory = tmp_path / "sessions"
    store = SessionStore(directory)
    store.save("base", [], root=tmp_path)
    scan_complete = context.Event()
    release_scan = context.Event()
    prune = context.Process(
        target=_prune_with_paused_session_scan,
        args=(str(directory), scan_complete, release_scan),
    )
    writer = context.Process(
        target=_save_session_in_process,
        args=(str(directory), "concurrent"),
    )
    try:
        prune.start()
        assert scan_complete.wait(timeout=10)
        writer.start()
        concurrent_path = store._session_path("concurrent")
        deadline = time.monotonic() + 10
        while not concurrent_path.exists() and time.monotonic() < deadline:
            writer.join(timeout=0.01)
        assert concurrent_path.exists()
        assert writer.is_alive(), "writer should still be waiting on the index lock"
        release_scan.set()
        prune.join(timeout=15)
        writer.join(timeout=15)
    finally:
        release_scan.set()
        for process in (prune, writer):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert prune.exitcode == 0
    assert writer.exitcode == 0
    restarted = SessionStore(directory)
    assert set(restarted._load_index().sessions) == {"base", "concurrent"}
    assert restarted.load("concurrent").messages[-1].text_content() == (
        "created while maintenance held the index lock"
    )


def test_prune_without_directory_snapshot_keeps_direct_missing_file_check(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("missing", [], root=tmp_path)
    index = store._load_index().model_copy(deep=True)
    store._session_path("missing").unlink()

    changed = prune_index_and_sessions(
        store,
        index=index,
        retention_days=30,
        exclude_session_id=None,
    )

    assert changed is True
    assert "missing" not in index.sessions


def test_session_save_survives_and_repairs_an_index_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.session.index_cache as index_cache_module

    store = SessionStore(tmp_path / "sessions")
    original_replace = store._index_cache._replace_once
    replace_attempts = 0

    def deny_replace(_temporary: Path) -> None:
        nonlocal replace_attempts
        replace_attempts += 1
        raise PermissionError("index is locked")

    monkeypatch.setattr(store._index_cache, "_replace_once", deny_replace)
    monkeypatch.setattr(index_cache_module.time, "sleep", lambda _seconds: None)

    saved = store.save(
        "recoverable-index",
        [Message.user("persist this turn")],
        root=tmp_path,
    )

    assert store._session_path(saved.id).exists()
    assert store.load(saved.id).messages[-1].text_content() == "persist this turn"
    assert store.index_entry(saved.id) is not None
    assert not store._index_path().exists()
    assert replace_attempts == INDEX_WRITE_ATTEMPTS
    assert not list(store.directory.glob(".index.json.*.tmp"))

    monkeypatch.setattr(store._index_cache, "_replace_once", original_replace)
    store.maintain_index(force=True)

    assert store._index_path().exists()
    assert SessionStore(store.directory).index_entry(saved.id) is not None


def _add_index_entry(index: SessionIndex, entry: SessionIndexEntry) -> bool:
    index.sessions[entry.id] = entry
    return True
