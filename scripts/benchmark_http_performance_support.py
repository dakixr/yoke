"""Benchmark Yoke HTTP behavior against a synthetic large session store.

This is a wall-clock benchmark, not a correctness test. It starts a real
``yoke serve`` subprocess, talks to it over loopback HTTP, and reports both
startup and request latency. The fixture is deterministic enough to compare
before/after changes on the same machine without requiring a user's sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
from threading import Thread
import time
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.session import SessionStore
from yoke.session.events import SessionEventJournal
from yoke.session.legacy.importer import import_legacy_sessions


TOKEN = "yoke-perf-benchmark"


@dataclass(slots=True)
class Timing:
    name: str
    seconds: float
    status: int | None = None
    bytes: int | None = None


class ServerProcess:
    def __init__(self, session_dir: Path, *, open_browser: bool) -> None:
        self.session_dir = session_dir
        self.open_browser = open_browser
        self.process: subprocess.Popen[str] | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.port: int | None = None
        self.started_at = 0.0
        self.listening_at: float | None = None
        self.opening_at: float | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["YOKE_SESSION_DIR"] = str(self.session_dir)
        env["PYTHONUNBUFFERED"] = "1"
        if self.open_browser:
            env["BROWSER"] = shutil.which("true") or "/bin/true"
        repo_root = Path(__file__).resolve().parents[1]
        local_yoke = repo_root / ".venv" / "bin" / "yoke"
        command = [
            str(local_yoke) if local_yoke.exists() else "yoke",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--token",
            TOKEN,
        ]
        if self.open_browser:
            command.append("--open")
        self.started_at = time.perf_counter()
        self.process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.process.stdout is not None
        Thread(
            target=self._read_output, args=(self.process.stdout,), daemon=True
        ).start()

    def wait_ready(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    f"yoke serve exited early with {self.process.returncode}: "
                    + " | ".join(self._drain_lines())
                )
            try:
                line = self.lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line.startswith("Yoke HTTP listening on http://127.0.0.1:"):
                self.listening_at = time.perf_counter()
                self.port = int(line.rsplit(":", 1)[1])
            elif line.startswith("Opening Yoke web UI at "):
                self.opening_at = time.perf_counter()
            if self.port is not None and (
                not self.open_browser or self.opening_at is not None
            ):
                return
        raise TimeoutError("Timed out waiting for yoke serve startup")

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _read_output(self, stdout: Any) -> None:
        for line in stdout:
            self.lines.put(line.rstrip("\n"))

    def _drain_lines(self) -> list[str]:
        result: list[str] = []
        while True:
            try:
                result.append(self.lines.get_nowait())
            except queue.Empty:
                return result


def _entry_line(
    *, entry_id: str, parent_id: str | None, content: str, created_at: str
) -> str:
    return (
        json.dumps(
            {
                "type": "entry",
                "entry": {
                    "kind": "user",
                    "message": {
                        "role": "user",
                        "content": content,
                        "tool_call_id": None,
                        "tool_calls": [],
                        "phase": None,
                        "reasoning_content": None,
                        "reasoning_signature": None,
                        "usage": None,
                    },
                    "metadata": {},
                    "id": entry_id,
                    "parent_id": parent_id,
                    "created_at": created_at,
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def _write_session(
    path: Path,
    *,
    session_id: str,
    root: Path,
    entries: int,
    payload_bytes: int,
    archived: bool = False,
) -> None:
    timestamp = "2026-08-27T12:00:00+00:00"
    archived_at = timestamp if archived else None
    leaf_id = f"entry-{entries - 1:08d}" if entries else None
    metadata = {
        "type": "metadata",
        "version": 5,
        "id": session_id,
        "leaf_id": leaf_id,
        "active_skills": [],
        "skill_dirs": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "root": str(root),
        "title": f"Benchmark {session_id}",
        "pinned": False,
        "archived_at": archived_at,
        "provider_name": "codex",
        "model_id": "gpt-5.6-codex",
        "reasoning_effort": "medium",
        "context_window_tokens": None,
    }
    content = "x" * payload_bytes
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type":"yoke_session","version":2}\n')
        handle.write(json.dumps(metadata, separators=(",", ":")) + "\n")
        parent: str | None = None
        for index in range(entries):
            entry_id = f"entry-{index:08d}"
            handle.write(
                _entry_line(
                    entry_id=entry_id,
                    parent_id=parent,
                    content=content,
                    created_at=timestamp,
                )
            )
            parent = entry_id


def _write_events(session_dir: Path, *, session_id: str, events: int) -> None:
    journal = SessionEventJournal(session_dir)
    with journal.database.write():
        for seq in range(1, events + 1):
            journal.append(
                session_id,
                "session.message.updated",
                {"entryID": f"entry-{seq:08d}"},
            )


def _append_session_entry(
    session_dir: Path,
    *,
    session_id: str,
    parent_id: str,
    entry_id: str,
) -> None:
    """Append one entry from this (separate) process while the server runs."""
    store = SessionStore(session_dir)
    summary = store.summary_record(session_id)
    assert summary is not None
    entry = ConversationEntry(
        kind="user",
        id=entry_id,
        parent_id=parent_id,
        message=Message.user("incremental refresh"),
    )
    store.save_indexed_tree_navigation(
        session_id,
        existing_record=summary,
        leaf_id=entry_id,
        appended_entries=(entry,),
    )


def build_fixture(
    directory: Path,
    *,
    session_count: int,
    large_mib: int,
    event_count: int,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "workspace"
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    legacy = directory / "legacy-jsonl"
    legacy.mkdir(exist_ok=True)
    for index in range(session_count):
        session_id = f"small-{index:05d}"
        _write_session(
            legacy / f"{session_id}.jsonl",
            session_id=session_id,
            root=root,
            entries=1,
            payload_bytes=32,
            archived=index % 5 == 0,
        )

    bytes_per_entry = 8192
    large_entries = max(1, large_mib * 1024 * 1024 // (bytes_per_entry + 512))
    large_id = "large-session"
    _write_session(
        legacy / f"{large_id}.jsonl",
        session_id=large_id,
        root=root,
        entries=large_entries,
        payload_bytes=bytes_per_entry,
    )
    store = SessionStore(directory)
    results = list(import_legacy_sessions(store, legacy))
    assert all(result.status == "imported" for result in results), results
    shutil.rmtree(legacy)
    _write_events(directory, session_id=large_id, events=event_count)
    signature = store.session_file_signature(large_id)
    assert signature is not None
    return {
        "largeSessionID": large_id,
        "root": str(root),
        "sessionCount": len(results),
        "largeEntries": large_entries,
        "largeBytes": signature[0],
        "eventCount": event_count,
        "databaseBytes": store.database_path.stat().st_size,
    }


def _request(port: int, path: str) -> Timing:
    url = f"http://127.0.0.1:{port}{path}"
    request = Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    start = time.perf_counter()
    with urlopen(request, timeout=180) as response:  # noqa: S310
        body = response.read()
        status = response.status
    return Timing(
        name=path,
        seconds=time.perf_counter() - start,
        status=status,
        bytes=len(body),
    )


def _json_request(
    port: int,
    path: str,
    *,
    method: str,
    payload: dict[str, object],
    label: str | None = None,
) -> Timing:
    url = f"http://127.0.0.1:{port}{path}"
    request = Request(
        url,
        method=method,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    start = time.perf_counter()
    with urlopen(request, timeout=180) as response:  # noqa: S310
        body = response.read()
        status = response.status
    return Timing(
        name=label or f"{method} {path}",
        seconds=time.perf_counter() - start,
        status=status,
        bytes=len(body),
    )
