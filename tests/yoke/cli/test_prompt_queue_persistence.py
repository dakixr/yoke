from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from threading import Lock

import pytest

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.queue.persistence import clear_prompt_queue
from yoke.cli.interactive.queue.persistence import load_prompt_queue
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.queue import load_prompt_queue_snapshots
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import write_prompt_queue_snapshot

from .support import active_session_for


def test_batch_queue_load_enumerates_the_queue_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_directory = tmp_path / "sessions"
    write_prompt_queue_snapshot(
        session_directory,
        "queued",
        PersistedPromptQueue(revision=7),
    )
    queue_directory = session_directory / "queues"
    (queue_directory / "corrupt.json").write_text("{", encoding="utf-8")
    calls = 0
    original_iterdir = Path.iterdir

    def count_iterdir(path: Path):
        nonlocal calls
        if path == queue_directory:
            calls += 1
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", count_iterdir)

    snapshots = load_prompt_queue_snapshots(
        session_directory,
        ["missing", "queued", "corrupt"],
    )

    assert calls == 1
    assert snapshots["missing"].revision == 0
    assert snapshots["queued"].revision == 7
    assert snapshots["corrupt"].revision == 0


def test_batch_queue_load_falls_back_when_directory_enumeration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_directory = tmp_path / "sessions"
    write_prompt_queue_snapshot(
        session_directory,
        "queued",
        PersistedPromptQueue(revision=7),
    )
    queue_directory = session_directory / "queues"
    original_iterdir = Path.iterdir

    def fail_queue_iterdir(path: Path):
        if path == queue_directory:
            raise PermissionError("directory listing denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_queue_iterdir)

    snapshots = load_prompt_queue_snapshots(
        session_directory,
        ["missing", "queued"],
    )

    assert snapshots["missing"].revision == 0
    assert snapshots["queued"].revision == 7


def test_batch_queue_load_stays_empty_when_fallback_reads_also_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_directory = tmp_path / "sessions"
    queue_directory = session_directory / "queues"
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text

    def fail_queue_iterdir(path: Path):
        if path == queue_directory:
            raise PermissionError("directory listing denied")
        return original_iterdir(path)

    def fail_queue_read(path: Path, *args, **kwargs):
        if path.parent == queue_directory:
            raise PermissionError("queue read denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", fail_queue_iterdir)
    monkeypatch.setattr(Path, "read_text", fail_queue_read)

    snapshots = load_prompt_queue_snapshots(
        session_directory,
        ["first", "second"],
    )

    assert snapshots["first"].revision == 0
    assert snapshots["second"].revision == 0


def test_batch_queue_load_treats_a_missing_queue_directory_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.session.queue as queue_module

    def fail_single_load(*_args, **_kwargs):
        raise AssertionError("a missing queue directory must not fan out path probes")

    monkeypatch.setattr(queue_module, "load_prompt_queue_snapshot", fail_single_load)

    snapshots = load_prompt_queue_snapshots(
        tmp_path / "sessions",
        ["first", "second"],
    )

    assert snapshots["first"].revision == 0
    assert snapshots["second"].revision == 0


def test_batch_queue_load_matches_case_insensitive_windows_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.session.queue as queue_module

    session_directory = tmp_path / "sessions"
    queue_directory = session_directory / "queues"
    queue_directory.mkdir(parents=True)
    (queue_directory / "Queued.JSON").write_text(
        PersistedPromptQueue(revision=9).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue_module.os.path,
        "normcase",
        lambda value: str(value).casefold(),
    )

    snapshots = load_prompt_queue_snapshots(session_directory, ["queued"])

    assert snapshots["queued"].revision == 9


def test_consumed_prompt_is_removed_from_persisted_queue(tmp_path) -> None:
    active_session = active_session_for(tmp_path)
    state = PromptCliState(
        messages=[],
        pending_prompts=[
            PendingPrompt("consumed"),
            PendingPrompt("still queued"),
        ],
    )
    persist_prompt_queue(active_session, state.pending_prompts)

    next_prompt, _should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        request_context_usage=lambda _prompt: None,
    )

    restored_prompts, _restored_images = load_prompt_queue(active_session)
    assert next_prompt is not None
    assert next_prompt.prompt == "consumed"
    assert [prompt.prompt for prompt in state.pending_prompts] == ["still queued"]
    assert [prompt.prompt for prompt in restored_prompts] == ["still queued"]

    final_prompt, _should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        request_context_usage=lambda _prompt: None,
    )

    restored_prompts, _restored_images = load_prompt_queue(active_session)
    assert final_prompt is not None
    assert final_prompt.prompt == "still queued"
    assert restored_prompts == []


def test_unchanged_prompt_queue_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_session = active_session_for(tmp_path)
    prompts = [PendingPrompt("still queued")]
    persist_prompt_queue(active_session, prompts)
    writes = {"count": 0}
    original_write_text = Path.write_text

    def count_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        writes["count"] += 1
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", count_write)

    persist_prompt_queue(active_session, prompts)

    assert writes["count"] == 0


def test_clearing_prompt_queue_preserves_monotonic_revision(tmp_path: Path) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(active_session, [PendingPrompt("first")])
    before = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    assert before.revision == 1

    clear_prompt_queue(active_session)

    cleared = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    assert cleared.revision == 2
    assert cleared.prompts == []

    persist_prompt_queue(active_session, [PendingPrompt("second")])
    restored = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    assert restored.revision == 3
    assert [item.prompt for item in restored.prompts] == ["second"]
