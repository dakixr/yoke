from __future__ import annotations

# ruff: noqa: D100,D103,S101

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from yoke.agent.models import Message
from yoke.cli.session import SessionStore
from yoke.cli.session.index_cache import INDEX_WRITE_ATTEMPTS
from yoke.cli.session.index_cache import INDEX_WRITE_RETRY_SECONDS
from yoke.cli.session.index_cache import SessionIndexCache
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
