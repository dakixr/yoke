from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from threading import Lock

from yoke.session.admissions import AdmissionRecord
from yoke.session.admissions import AdmissionSnapshot
from yoke.session.admissions import AdmissionStore


def test_concurrent_admission_writes_use_distinct_temporary_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_directory = tmp_path / "sessions"
    destination = session_directory / "inputs" / "shared.json"
    replacements = Barrier(2)
    temporary_paths: list[Path] = []
    paths_lock = Lock()
    original_replace = Path.replace

    def synchronize_replace(path: Path, target: Path) -> Path:
        if target == destination:
            with paths_lock:
                temporary_paths.append(path)
            replacements.wait(timeout=5)
        return original_replace(path, target)

    def save(input_id: str) -> None:
        record = AdmissionRecord(
            id=input_id,
            session_id="shared",
            prompt=input_id,
            delivery="queue",
            fingerprint=input_id,
            time_created="2026-01-01T00:00:00+00:00",
            admitted_seq=1,
        )
        AdmissionStore(session_directory).save(
            "shared",
            AdmissionSnapshot(records={input_id: record}),
        )

    monkeypatch.setattr(Path, "replace", synchronize_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(save, "first")
        second = executor.submit(save, "second")
        first.result(timeout=5)
        second.result(timeout=5)

    snapshot = AdmissionStore(session_directory).load("shared")
    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert set(snapshot.records) in [{"first"}, {"second"}]
    assert not [path for path in destination.parent.iterdir() if path.suffix == ".tmp"]
