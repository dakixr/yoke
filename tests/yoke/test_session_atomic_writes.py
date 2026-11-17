from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from yoke.session import SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.admissions import AdmissionSnapshot
from yoke.session.admissions import AdmissionStore


def test_concurrent_admission_saves_each_commit_a_whole_snapshot(
    tmp_path: Path,
) -> None:
    session_directory = tmp_path / "sessions"
    SessionStore(session_directory)
    start = Barrier(2)

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
        start.wait(timeout=5)
        AdmissionStore(session_directory).save(
            "shared",
            AdmissionSnapshot(records={input_id: record}),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(save, "first")
        second = executor.submit(save, "second")
        first.result(timeout=5)
        second.result(timeout=5)

    snapshot = AdmissionStore(session_directory).load("shared")
    assert set(snapshot.records) in [{"first"}, {"second"}]
    assert sorted(path.name for path in session_directory.iterdir()) == [
        "yoke.db",
        "yoke.db-shm",
        "yoke.db-wal",
    ]
