"""Acknowledged WAL writes survive SIGKILL; queue revisions exclude stale edits."""

from __future__ import annotations

import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from tests.yoke.storage.test_qa_regressions import pending_service
from yoke.agent.models import Message
from yoke.http.models.prompt import QueuePatchRequest
from yoke.session import SessionStore
from yoke.session.queue import load_prompt_queue_snapshot


def acknowledged(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready, "child did not acknowledge its committed state"
    return json.loads(process.stdout.readline())


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGKILL durability requires POSIX kill semantics on Linux/macOS",
)
def test_acknowledged_entry_title_and_queue_survive_sigkill(tmp_path: Path) -> None:
    script = """
import json, sys
from pathlib import Path
from yoke.agent.models import Message
from yoke.session import SessionStore
from yoke.session.queue import PersistedPendingInput, prompt_queue_transaction
store = SessionStore(Path(sys.argv[1]), migrate=True)
record = store.save("durable", [Message.user("acknowledged entry")])
store.set_title("durable", "acknowledged title")
with prompt_queue_transaction(store.directory, "durable") as transaction:
    transaction.snapshot.revision = 1
    transaction.snapshot.prompts.append(PersistedPendingInput(id="queued", prompt="acknowledged queue", created_at="2026-11-17"))
    transaction.commit()
print(json.dumps({"entry_id": record.conversation_entries[0].id, "revision": 1}), flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ack = acknowledged(process)
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -9
        store = SessionStore(tmp_path, migrate=True)
        record = store.load("durable")
        assert record.messages == [Message.user("acknowledged entry")]
        assert record.conversation_entries[0].id == ack["entry_id"]
        assert record.title == "acknowledged title"
        queue = load_prompt_queue_snapshot(store.directory, "durable")
        assert queue.revision == ack["revision"]
        assert [(item.id, item.prompt) for item in queue.prompts] == [
            ("queued", "acknowledged queue")
        ]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_two_process_queue_revision_conflict_preserves_both_edits(
    tmp_path: Path,
) -> None:
    # communicate() works on Windows too; only the SIGKILL test is platform-specific.
    store = SessionStore(tmp_path, migrate=True)
    store.save("shared", [], root=tmp_path)
    script = """
import json, sys
from pathlib import Path
from tests.yoke.storage.test_qa_regressions import pending_service
from yoke.http.errors import ApiError
from yoke.http.models.prompt import QueuePatchRequest
from yoke.session import SessionStore
from yoke.session.queue import load_prompt_queue_snapshot
store = SessionStore(Path(sys.argv[1]), migrate=True)
service = pending_service(store)
snapshot = load_prompt_queue_snapshot(store.directory, "shared")
print(json.dumps({"revision": snapshot.revision}), flush=True)
sys.stdin.readline()
try:
    service.patch_queue("shared", QueuePatchRequest(expected_revision=snapshot.revision, operations=[], pending_images=["child"]))
except ApiError as exc:
    conflict = exc.code
else:
    raise AssertionError("stale queue edit was accepted")
fresh = load_prompt_queue_snapshot(store.directory, "shared")
result = service.patch_queue("shared", QueuePatchRequest(expected_revision=fresh.revision, operations=[], pending_images=[*fresh.pending_images, "child"]))
print(json.dumps({"conflict": conflict, "revision": result.revision}), flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # A single explicit handshake ensures both processes read revision zero.
        assert process.stdout is not None
        ready = json.loads(process.stdout.readline())
        assert ready == {"revision": 0}
        result = pending_service(store).patch_queue(
            "shared",
            QueuePatchRequest(
                expected_revision=0, operations=[], pending_images=["parent"]
            ),
        )
        assert result.revision == 1
        output, errors = process.communicate("go\n", timeout=10)
        assert process.returncode == 0, errors
        assert json.loads(output) == {
            "conflict": "queue_revision_conflict",
            "revision": 2,
        }
        queue = load_prompt_queue_snapshot(store.directory, "shared")
        assert queue.revision == 2
        assert queue.pending_images == ["parent", "child"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
