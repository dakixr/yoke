from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from threading import Event

from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.queue import prompt_queue_transaction
from yoke.session.queue import write_prompt_queue_snapshot


def test_interleaved_queue_transactions_preserve_both_mutations(
    tmp_path: Path,
) -> None:
    session_directory = tmp_path / "sessions"
    first_holds_lock = Event()
    second_is_ready = Event()

    def append_first() -> None:
        with prompt_queue_transaction(session_directory, "shared") as transaction:
            first_holds_lock.set()
            assert second_is_ready.wait(timeout=5)
            transaction.snapshot.prompts.append(_pending("first"))
            transaction.snapshot.revision += 1
            transaction.commit()

    def append_second() -> None:
        assert first_holds_lock.wait(timeout=5)
        second_is_ready.set()
        with prompt_queue_transaction(session_directory, "shared") as transaction:
            transaction.snapshot.prompts.append(_pending("second"))
            transaction.snapshot.revision += 1
            transaction.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(append_first)
        second = executor.submit(append_second)
        first.result(timeout=5)
        second.result(timeout=5)

    snapshot = load_prompt_queue_snapshot(session_directory, "shared")
    assert snapshot.revision == 2
    assert [item.id for item in snapshot.prompts] == ["first", "second"]


def test_concurrent_queue_writes_use_distinct_temporary_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_directory = tmp_path / "sessions"
    destination = session_directory / "queues" / "shared.json"
    start = Barrier(3)
    temporary_paths: list[Path] = []
    original_replace = Path.replace

    def capture_replace(path: Path, target: Path) -> Path:
        if target == destination:
            temporary_paths.append(path)
        return original_replace(path, target)

    def write(input_id: str, revision: int) -> None:
        start.wait(timeout=5)
        write_prompt_queue_snapshot(
            session_directory,
            "shared",
            snapshot=PersistedPromptQueue(
                revision=revision,
                prompts=[_pending(input_id)],
            ),
        )

    monkeypatch.setattr(Path, "replace", capture_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "first", 1)
        second = executor.submit(write, "second", 2)
        start.wait(timeout=5)
        first.result(timeout=5)
        second.result(timeout=5)

    snapshot = load_prompt_queue_snapshot(session_directory, "shared")
    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert snapshot.revision in {1, 2}
    assert [item.id for item in snapshot.prompts] in [["first"], ["second"]]
    assert not [path for path in destination.parent.iterdir() if path.suffix == ".tmp"]


def _pending(input_id: str) -> PersistedPendingInput:
    return PersistedPendingInput(
        id=input_id,
        prompt=input_id,
        created_at="2026-01-01T00:00:00+00:00",
    )
