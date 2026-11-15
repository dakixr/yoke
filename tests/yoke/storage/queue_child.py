"""Child process editing one session queue concurrently with another process.

Usage: python queue_child.py ROLE SESSION_DIR SESSION_ID COUNT START_FILE

ROLE "cli" replaces the whole queue through the CLI persistence helpers with
an expected revision. ROLE "daemon" admits inputs and edits one input through
the HTTP PendingInputService. Both retry on their revision conflicts and print
one JSON summary line.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from yoke.cli.runtime import ActiveSession


def wait_for(start: Path) -> None:
    print("ready", flush=True)
    deadline = time.monotonic() + 30
    while not start.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("start signal never arrived")
        time.sleep(0.001)


def run_cli(directory: Path, session_id: str, count: int, start: Path) -> dict:
    from yoke.cli.interactive.common import PendingPrompt
    from yoke.cli.interactive.queue.persistence import (
        PromptQueueRevisionConflict,
        commit_prompt_queue,
        load_prompt_queue_state,
    )
    from yoke.session import SessionStore

    active = cast(
        "ActiveSession",
        SimpleNamespace(store=SessionStore(directory), id=session_id),
    )
    conflicts = 0
    wait_for(start)
    for index in range(count):
        while True:
            loaded = load_prompt_queue_state(active)
            prompts = [
                *loaded.prompts,
                PendingPrompt(prompt=f"cli {index}", id=f"cli-{index}"),
            ]
            try:
                commit_prompt_queue(
                    active,
                    prompts,
                    loaded.pending_images,
                    expected_revision=loaded.revision,
                )
                break
            except PromptQueueRevisionConflict:
                conflicts += 1
    return {"role": "cli", "commits": count, "conflicts": conflicts}


def run_daemon(directory: Path, session_id: str, count: int, start: Path) -> dict:
    from yoke.http.errors import ApiError
    from yoke.http.models.prompt import PromptAdmissionRequest, QueuePatchRequest
    from yoke.http.services.event_broker import EventService, GlobalEventBroker
    from yoke.http.services.pending_input_service import PendingInputService
    from yoke.http.services.upload_service import UploadService
    from yoke.session import SessionStore
    from yoke.session.admissions import AdmissionStore
    from yoke.session.events import SessionEventJournal

    store = SessionStore(directory)
    service = PendingInputService(
        store,
        AdmissionStore(store.directory),
        EventService(SessionEventJournal(store.directory), GlobalEventBroker()),
        UploadService(store),
    )
    conflicts = 0
    codes: set[str] = set()
    wait_for(start)
    for index in range(count):
        service.admit(
            session_id,
            PromptAdmissionRequest.model_validate(
                {
                    "id": f"daemon-{index}",
                    "prompt": {"text": f"daemon {index}"},
                    "delivery": "queue",
                }
            ),
        )
        while True:
            revision = service.queue(session_id).revision
            try:
                service.patch_queue(
                    session_id,
                    QueuePatchRequest.model_validate(
                        {
                            "expectedRevision": revision,
                            "operations": [
                                {
                                    "op": "update",
                                    "id": "daemon-0",
                                    "prompt": {"text": f"daemon edit {index}"},
                                }
                            ],
                        }
                    ),
                )
                break
            except ApiError as exc:
                if exc.status_code != 409:
                    raise
                codes.add(exc.code)
                conflicts += 1
    return {
        "role": "daemon",
        "commits": 2 * count,
        "conflicts": conflicts,
        "codes": sorted(codes),
    }


def main() -> None:
    role, directory, session_id, count, start = sys.argv[1:6]
    runner = run_cli if role == "cli" else run_daemon
    summary = runner(Path(directory), session_id, int(count), Path(start))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
