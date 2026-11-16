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


def wait_for(start: Path) -> None:
    print("ready", flush=True)
    wait_for_path(start)


def wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"signal never arrived: {path.name}")
        time.sleep(0.001)


def run_terminal_writer(
    directory: Path, session_id: str, count: int, start: Path
) -> dict:
    from contextlib import closing
    from tests.yoke.cli.daemon_support import serve_app
    from yoke.cli.backend.connection import SessionConnection
    from yoke.cli.backend.daemon import DaemonSessionBackend
    from yoke.cli.backend.models import PendingPrompt
    from yoke.cli.backend.values import PromptQueueRevisionConflict
    from yoke.http.app import create_app, HttpAppSettings
    from yoke.session import SessionStore

    from yoke.session.workspace import require_session_workspace

    root = require_session_workspace(SessionStore(directory).load(session_id))
    app = create_app(HttpAppSettings(auth_token=None, session_directory=directory))
    with (
        serve_app(app) as handle,
        closing(
            SessionConnection(session_id, discover=lambda: handle, cwd=root)
        ) as connection,
    ):
        connection.start()
        backend = DaemonSessionBackend(
            connection, connection.session_request("GET")["data"]
        )
        conflicts = 0
        wait_for(start)
        for index in range(count):
            if index == 0:
                wait_for_path(start.with_name("daemon-admitted"))
            first_attempt = True
            while True:
                loaded = backend.load_queue()
                if index == 0 and first_attempt:
                    # Force one shared revision, rather than relying on timing.
                    start.with_name("cli-read").touch()
                    wait_for_path(start.with_name("daemon-read"))
                    first_attempt = False
                prompts = [
                    *loaded.prompts,
                    PendingPrompt(prompt=f"cli {index}", id=f"cli-{index}"),
                ]
                try:
                    backend.commit_queue(
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
        if index == 0:
            start.with_name("daemon-admitted").touch()
        first_attempt = True
        while True:
            revision = service.queue(session_id).revision
            if index == 0 and first_attempt:
                start.with_name("daemon-read").touch()
                wait_for_path(start.with_name("cli-read"))
                first_attempt = False
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
    runner = run_terminal_writer if role == "cli" else run_daemon
    summary = runner(Path(directory), session_id, int(count), Path(start))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
