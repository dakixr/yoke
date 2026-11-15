"""A CLI process and a daemon process editing one session queue concurrently."""

from __future__ import annotations

# ruff: noqa: S101,S603

import json
import os
import subprocess
import sys
from pathlib import Path

from yoke.agent.models import Message
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.queue import load_prompt_queue_snapshot

CHILD = Path(__file__).with_name("queue_child.py")
COUNT = 60


def test_cli_and_daemon_queue_edits_conflict_and_never_lose_updates(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(directory)
    store.save("shared", [Message.user("hello")], root=workspace)
    start = tmp_path / "start"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    children = [
        subprocess.Popen(
            [sys.executable, str(CHILD), role, str(directory), "shared", str(COUNT)]
            + [str(start)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for role in ("cli", "daemon")
    ]
    for child in children:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    start.touch()
    summaries = {}
    for child in children:
        stdout, stderr = child.communicate(timeout=180)
        assert child.returncode == 0, stderr
        summary = json.loads(stdout.strip().splitlines()[-1])
        summaries[summary["role"]] = summary

    print(json.dumps(summaries))
    queue = load_prompt_queue_snapshot(directory, "shared")
    ids = [item.id for item in queue.prompts]
    assert sorted(ids) == sorted(
        [f"cli-{index}" for index in range(COUNT)]
        + [f"daemon-{index}" for index in range(COUNT)]
    )
    texts = {item.id: item.prompt for item in queue.prompts}
    assert texts["daemon-0"] == f"daemon edit {COUNT - 1}"
    assert all(texts[f"cli-{index}"] == f"cli {index}" for index in range(COUNT))
    # Every successful commit bumped the revision exactly once.
    assert (
        queue.revision == summaries["cli"]["commits"] + summaries["daemon"]["commits"]
    )
    admissions = AdmissionStore(directory).load("shared").records
    assert sorted(admissions) == sorted(f"daemon-{index}" for index in range(COUNT))
    assert admissions["daemon-0"].prompt == f"daemon edit {COUNT - 1}"
    # Conflicts surface as the errors each client already handles.
    assert summaries["cli"]["conflicts"] + summaries["daemon"]["conflicts"] > 0
    assert set(summaries["daemon"]["codes"]) <= {"queue_revision_conflict"}
