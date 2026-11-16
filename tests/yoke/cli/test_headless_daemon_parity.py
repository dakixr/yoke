"""Compare daemon headless output with frozen, verified local-path fixtures."""

import json
from pathlib import Path

import pytest
import httpx

from tests.yoke.provider_projection.golden import Normalizer, encode
from tests.yoke.provider_projection.recording import Tape, answer, tool_calls
from tests.yoke.provider_projection.runtime import World
from yoke.ai.providers.base import ProviderError
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.headless import run_headless_prompt, upload_images
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore

from .daemon_support import serve_app
from .support import CaptureStream


@pytest.mark.parametrize(
    "case", ["text", "empty", "error", "tool", "skill", "image", "image_unsupported"]
)
def test_daemon_headless_matches_frozen_stdout_stderr_exit_and_request_bytes(
    tmp_path, monkeypatch, case
):
    directory = tmp_path / "daemon"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(directory))
    tape = Tape()
    scenario = {"tool": "tools", "skill": "inline_skill"}.get(case, "chat")
    if case == "image_unsupported":
        scenario = "images_unsupported"
    world = World(tmp_path / "workspace", directory, scenario, tape)
    prompt = "Preserve café, [brackets], two  spaces.\nAnd a newline."
    images = ()
    if case.startswith("image"):
        images = (world.root / "tiny.png",)
    if case == "tool":
        tape.expect(tool_calls(("read", {"path": "input.txt"})))
    if case == "skill":
        prompt = "Use $oracle exactly."
    tape.expect(
        ProviderError("Fixture provider failed.")
        if case == "error"
        else answer("" if case == "empty" else "Exact response.\nSecond line.")
    )
    stdout, stderr = CaptureStream(), CaptureStream()
    app = create_app(
        HttpAppSettings(
            auth_token=None,
            session_directory=directory,
            agent_factory=world.agent,
        )
    )
    with serve_app(app) as handle:
        connection = SessionConnection(
            "oracle-session", discover=lambda: handle, cwd=world.root
        )
        try:
            connection.request(
                "POST",
                "session",
                json={
                    "id": connection.session_id,
                    "title": "Oracle fixture",
                    "location": {"directory": str(world.root)},
                },
            )
            code = run_headless_prompt(
                connection,
                prompt,
                image_paths=images,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            connection.close()
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "headless" / f"{case}.json").read_text()
    )
    assert (stdout.getvalue().encode(), stderr.getvalue().encode(), code) == (
        expected["stdout"].encode(),
        expected["stderr"].encode(),
        expected["exit"],
    )
    normalizer = Normalizer(world.root)
    normalizer.register(SessionStore(directory).load("oracle-session"))
    assert encode(normalizer.normalize(tape.calls)) == expected["requests"].encode()
    assert not tape.steps


def test_headless_replays_uncertain_admission_once_without_duplicate_provider_call(
    tmp_path, monkeypatch
):
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, "chat", tape)
    tape.expect(answer("One answer."))
    app = create_app(
        HttpAppSettings(
            auth_token=None, session_directory=directory, agent_factory=world.agent
        )
    )
    with serve_app(app) as handle:
        connection = SessionConnection(
            "oracle-session", discover=lambda: handle, cwd=world.root
        )
        try:
            connection.request(
                "POST",
                "session",
                json={
                    "id": connection.session_id,
                    "title": "Oracle fixture",
                    "location": {"directory": str(world.root)},
                },
            )
            request = connection.session_request
            attempts = []

            def lose_first_receipt(method, suffix="", **kwargs):
                result = request(method, suffix, **kwargs)
                if suffix == "/prompt":
                    attempts.append(kwargs["json"]["id"])
                    if len(attempts) == 1:
                        raise httpx.ReadError(
                            "Lost response after acknowledged admission"
                        )
                return result

            monkeypatch.setattr(connection, "session_request", lose_first_receipt)
            stdout, stderr = CaptureStream(), CaptureStream()
            assert (
                run_headless_prompt(
                    connection, "One prompt.", stdout=stdout, stderr=stderr
                )
                == 0
            )
            assert stdout.getvalue() == "One answer.\n"
            assert stderr.getvalue() == ""
            assert len(attempts) == 2 and len(set(attempts)) == 1
            assert len(tape.calls) == 1
            assert not tape.steps
            page = connection.session_request(
                "GET", "/message", params={"order": "asc"}
            )
            assert [item["type"] for item in page["data"]] == ["user", "assistant"]
        finally:
            connection.close()


def test_cli_image_projection_survives_queue_edit_and_delivery_change(
    tmp_path, monkeypatch
):
    import json

    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, "chat", tape)
    tape.expect(answer("Edited image answer."))
    app = create_app(
        HttpAppSettings(
            auth_token=None, session_directory=directory, agent_factory=world.agent
        )
    )
    with serve_app(app) as handle:
        connection = SessionConnection(
            "oracle-session", discover=lambda: handle, cwd=world.root
        )
        try:
            connection.request(
                "POST",
                "session",
                json={
                    "id": connection.session_id,
                    "title": "Oracle fixture",
                    "location": {"directory": str(world.root)},
                },
            )
            connection.start()
            attachments = upload_images(connection, (world.root / "tiny.png",))
            prompt = {"text": "Original image prompt.", "attachments": attachments}
            admitted = connection.session_request(
                "POST",
                "/prompt",
                json={
                    "id": "image-input",
                    "clientID": connection.owner,
                    "prompt": prompt,
                    "resume": False,
                },
            )["data"]
            prompt["text"] = "Edited image prompt."
            queue = connection.session_request("GET", "/queue")["data"]
            edited = connection.session_request(
                "PATCH",
                "/queue",
                json={
                    "expectedRevision": queue["revision"],
                    "operations": [
                        {"op": "update", "id": admitted["id"], "prompt": prompt},
                        {
                            "op": "setDelivery",
                            "id": admitted["id"],
                            "delivery": "queue",
                        },
                        {"op": "setPaused", "id": admitted["id"], "paused": True},
                    ],
                },
            )["data"]
            attachment = edited["items"][0]["prompt"]["attachments"][0]
            assert attachment["imageProjection"] == "cli"
            connection.session_request(
                "PATCH",
                "/queue",
                json={
                    "expectedRevision": edited["revision"],
                    "operations": [
                        {"op": "setPaused", "id": admitted["id"], "paused": False}
                    ],
                },
            )
            assert (
                connection.session_request("POST", "/wait", params={"timeoutMs": 5000})[
                    "data"
                ]["state"]
                == "idle"
            )
            assert len(tape.calls) == 1
            user = json.loads(tape.calls[0]["messages_json"][-1])
            assert user["content"][0]["text"] == "Edited image prompt."
            assert user["content"][1]["label"] == "[Image #1]"
            assert user["content"][1]["attachment_name"] is None
            assert not tape.steps
        finally:
            connection.close()


def test_headless_literal_skill_command_remains_provider_text(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, "chat", tape)
    tape.expect(answer("Literal command accepted."))
    app = create_app(
        HttpAppSettings(
            auth_token=None, session_directory=directory, agent_factory=world.agent
        )
    )
    with serve_app(app) as handle:
        connection = SessionConnection(
            "literal-command", discover=lambda: handle, cwd=world.root
        )
        try:
            connection.request(
                "POST",
                "session",
                json={
                    "id": connection.session_id,
                    "title": "Literal fixture",
                    "location": {"directory": str(world.root)},
                },
            )
            stdout, stderr = CaptureStream(), CaptureStream()
            assert (
                run_headless_prompt(
                    connection, "/skill example", stdout=stdout, stderr=stderr
                )
                == 0
            )
            assert stdout.getvalue() == "Literal command accepted.\n"
            assert stderr.getvalue() == ""
            assert len(tape.calls) == 1
            assert (
                json.loads(tape.calls[0]["messages_json"][-1])["content"]
                == "/skill example"
            )
            assert not tape.steps
        finally:
            connection.close()
