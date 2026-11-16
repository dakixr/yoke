"""Drive both terminal loops over a real, temporary Unix daemon listener."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Literal, cast

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.input.posix_pipe import PosixPipeInput
from prompt_toolkit.output import DummyOutput

from tests.yoke.daemon.support import eventually
from tests.yoke.provider_projection.recording import Tape
from tests.yoke.provider_projection.runtime import World
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.basic import run_basic_interactive_cli
from yoke.cli.interactive.prompt.app import run_prompt_toolkit_cli
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.models.catalog import ModelCapabilities, ModelInfo, ModelListResponse
from yoke.http.models.common import LocationInfo

from .daemon_support import serve_app
from .support import CaptureStream


class InteractiveDaemon:
    def __init__(self, world: World) -> None:
        self.world = world
        self.tape = world.tape
        self.stack = ExitStack()
        self.app = create_app(
            HttpAppSettings(
                auth_token=None,
                session_directory=world.directory,
                agent_factory=world.agent,
            )
        )
        handle = self.stack.enter_context(serve_app(self.app))
        self.connection = SessionConnection(
            "oracle-session", discover=lambda: handle, cwd=world.root
        )
        self.stack.callback(self.connection.close)
        data = self.connection.request(
            "POST",
            "session",
            json={
                "id": self.connection.session_id,
                "title": "Oracle fixture",
                "selection": {"provider": "oracle", "model": "oracle-model"},
                "location": {"directory": str(world.root)},
            },
        )["data"]
        self.connection.start()
        from yoke.session import SessionStore

        SessionStore(world.directory).set_selection(
            self.connection.session_id,
            provider_name="oracle",
            model_id="oracle-model",
            reasoning_effort=None,
            context_window_tokens=100_000,
        )
        data = self.connection.session_request("GET")["data"]
        self.backend = DaemonSessionBackend(self.connection, data)
        self.stdout = CaptureStream()
        self.stderr = CaptureStream()
        self._pipe: PosixPipeInput | None = None
        self._inputs: Queue[str] = Queue()
        self.mode: Literal["basic", "prompt"] = "basic"

    def send(self, text: str) -> None:
        if self.mode == "basic":
            self._inputs.put(text)
        else:
            assert self._pipe is not None
            self._pipe.send_text(text + "\r")

    def keys(self, text: str) -> None:
        assert self._pipe is not None
        self._pipe.send_text(text)

    def wait_text(self, text: str, *, count: int = 1) -> None:
        try:
            eventually(lambda: self.stdout.getvalue().count(text) >= count, timeout=8)
        except AssertionError as exc:
            raise AssertionError(
                f"Missing {text!r} in screen:\n{self.stdout.getvalue()}"
            ) from exc

    def idle(self) -> None:
        status = self.connection.session_request(
            "POST", "/wait", params={"timeoutMs": 5000}
        )["data"]
        assert status["state"] == "idle", status

    def run(
        self,
        mode: Literal["basic", "prompt"],
        script: Callable[[InteractiveDaemon], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> str:
        self.mode = mode
        self.stdout = CaptureStream()
        errors: list[BaseException] = []

        def drive() -> None:
            try:
                script(self)
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.send("exit")

        with create_pipe_input() as pipe:
            self._pipe = cast(PosixPipeInput, pipe)
            worker = Thread(target=drive, daemon=True)
            worker.start()
            if mode == "basic":
                result = run_basic_interactive_cli(
                    self.backend,
                    self.backend.messages(),
                    active_session=cast_session(self.backend),
                    input_func=lambda _prompt="": self._inputs.get(timeout=12),
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
            else:
                monkeypatch.setattr("sys.stdout", self.stdout)
                result = run_prompt_toolkit_cli(
                    CLIArgs(root=str(self.world.root)),
                    self.backend,
                    self.backend.messages(),
                    active_session=cast_session(self.backend),
                    pt_input=pipe,
                    pt_output=DummyOutput(),
                )
            worker.join(timeout=10)
            assert not worker.is_alive()
            self._pipe = None
        if errors:
            errors[0].add_note(self.stdout.getvalue())
            raise errors[0]
        assert result == 0
        return self.stdout.getvalue()

    def close(self) -> None:
        self.stack.close()


def cast_session(backend: DaemonSessionBackend):
    return backend.session


@pytest.fixture
def interactive_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[InteractiveDaemon]:
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    world = World(tmp_path / "workspace", directory, "tools", Tape())
    daemon = InteractiveDaemon(world)

    def models(*, directory, provider, search):
        del provider, search
        return ModelListResponse(
            location=LocationInfo(directory=directory or str(world.root)),
            data=[
                ModelInfo(
                    id=model,
                    provider="oracle",
                    name=model,
                    reasoning_efforts=[],
                    capabilities=ModelCapabilities(images=True),
                    context_window_tokens=100_000,
                )
                for model in ("oracle-model", "oracle-vision")
            ],
        )

    monkeypatch.setattr(daemon.app.state.catalog_service, "models", models)
    try:
        yield daemon
    finally:
        daemon.close()
