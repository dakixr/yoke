"""kill -9 a writer at random points; every acknowledged write must survive."""

from __future__ import annotations

# ruff: noqa: S101,S311,S603

import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from yoke.session import SessionStore
from yoke.session.events import SessionEventJournal
from yoke.session.queue import load_prompt_queue_snapshot

CHILD = Path(__file__).with_name("writer_child.py")
ITERATIONS = 20


def _run_and_kill(directory: Path, session_id: str, iteration: int) -> list[str]:
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(directory), session_id, str(iteration)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert child.stdout is not None
    lines: list[str] = []
    ready = threading.Event()

    def read() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            if line.endswith("\n"):
                lines.append(line.strip())
                ready.set()

    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert ready.wait(60), child.stderr.read() if child.stderr else ""
        time.sleep(random.uniform(0.05, 0.6))
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(30)
        reader.join(30)
    assert child.returncode == -signal.SIGKILL
    assert lines[0] == "ready"
    return lines[1:]


def test_kill_nine_preserves_every_acknowledged_write(tmp_path: Path) -> None:
    random.seed(os.getpid())
    directory = tmp_path / "sessions"
    directory.mkdir()
    session_id = "durable"
    acked_entries: set[str] = set()
    last_title = (0, 0)
    last_queue = 0
    last_event = 0
    for iteration in range(1, ITERATIONS + 1):
        for line in _run_and_kill(directory, session_id, iteration):
            kind, *values = line.split()
            if kind == "entry":
                acked_entries.add(values[0])
            elif kind == "title":
                last_title = (int(values[0]), int(values[1]))
            elif kind == "queue":
                last_queue = int(values[0])
            elif kind == "event":
                last_event = int(values[0])
        _check_consistent(directory, session_id)

        store = SessionStore(directory)
        record = store.load(session_id)
        stored_ids = {entry.id for entry in record.conversation_entries}
        assert acked_entries <= stored_ids
        if last_title != (0, 0) and record.title:
            stored = tuple(int(part) for part in record.title.split()[1].split("."))
            assert stored >= last_title
        assert load_prompt_queue_snapshot(directory, session_id).revision >= last_queue
        assert SessionEventJournal(directory).latest_sequence(session_id) >= last_event
    assert len(acked_entries) > ITERATIONS


def _check_consistent(directory: Path, session_id: str) -> None:
    connection = sqlite3.connect(directory / "yoke.db")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        row = connection.execute(
            "SELECT entry_count, message_bytes, entry_seq, leaf_id "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert row is not None
        entry_count, message_bytes, entry_seq, leaf_id = row
        count, size, max_seq = connection.execute(
            "SELECT count(*), coalesce(sum(length(blobs.data)), 0), "
            "coalesce(max(seq), 0) FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert (entry_count, message_bytes) == (count, size)
        assert entry_seq >= max_seq
        ids = {
            entry_id: parent_id
            for entry_id, parent_id in connection.execute(
                "SELECT id, parent_id FROM entries WHERE session_id = ?",
                (session_id,),
            )
        }
        assert leaf_id is None or leaf_id in ids
        assert all(parent is None or parent in ids for parent in ids.values())
        queued = connection.execute(
            "SELECT count(*) FROM inputs WHERE session_id = ? AND position IS NOT NULL",
            (session_id,),
        ).fetchone()[0]
        assert queued <= 5
    finally:
        connection.close()
