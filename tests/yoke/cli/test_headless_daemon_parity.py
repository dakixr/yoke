"""Compare daemon headless output with frozen, verified local-path fixtures."""

import json
from pathlib import Path

import pytest

from tests.yoke.provider_projection.golden import Normalizer, encode
from tests.yoke.provider_projection.recording import Tape, answer, tool_calls
from tests.yoke.provider_projection.runtime import World
from yoke.ai.providers.base import ProviderError
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.headless import run_headless_prompt
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore

from .daemon_support import serve_app
from .support import CaptureStream


@pytest.mark.parametrize("case", ["empty", "error", "image_unsupported"])
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
