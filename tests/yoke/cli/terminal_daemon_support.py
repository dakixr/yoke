"""Run terminal entry points against an owned test daemon and runtime factory."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from functools import partial

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config.runtime import BuiltCLIAgent
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionRecord, SessionStore

from .daemon_support import serve_app


class ReplyProvider(Provider):
    """A provider fixture with separate title and ordinary-turn responses."""

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        if any(
            "Create a concise title" in (message.plain_text_content or "")
            for message in messages
        ):
            return Message.assistant("")
        return Message.assistant("synthetic response")


class TerminalDaemon:
    """Own a Unix listener, connections and real daemon-side RuntimeAgents."""

    def __init__(
        self,
        stack: ExitStack,
        monkeypatch: pytest.MonkeyPatch,
        factory: Callable[[SessionRecord], RuntimeAgent],
    ) -> None:
        self.stack = stack
        self.factory = factory
        self.store = SessionStore()
        self.app = create_app(
            HttpAppSettings(
                auth_token=None,
                daemon=DaemonLifecycle(idle_exit=False),
                session_directory=self.store.directory,
                agent_factory=lambda record: self.factory(record),
            )
        )
        self.handle = stack.enter_context(serve_app(self.app))
        monkeypatch.setattr(
            "yoke.cli.backend.startup.SessionConnection",
            partial(SessionConnection, discover=lambda: self.handle),
        )

        def startup(args, *, recover_model=False):
            del recover_model
            agent = self.factory(self.store.load(args.session))
            return BuiltCLIAgent(agent, ToolLoadReport([], [], []))

        monkeypatch.setattr(
            "yoke.http.services.session_startup.build_cli_agent_from_args", startup
        )

    def backend(self, session_id: str, *, root: Path) -> DaemonSessionBackend:
        connection = SessionConnection(
            session_id, discover=lambda: self.handle, cwd=root
        )
        self.stack.callback(connection.close)
        if self.store.exists(session_id):
            data = connection.session_request("GET")["data"]
        else:
            data = connection.request(
                "POST",
                "session",
                json={"id": session_id, "location": {"directory": str(root)}},
            )["data"]
        connection.start()
        return DaemonSessionBackend(connection, data)


@contextmanager
def terminal_daemon(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[SessionRecord], RuntimeAgent] | None = None,
) -> Iterator[TerminalDaemon]:
    """Never discover, start or connect to the host's production daemon."""
    with ExitStack() as stack:
        yield TerminalDaemon(
            stack,
            monkeypatch,
            factory
            or (
                lambda record: RuntimeAgent(
                    ReplyProvider(),
                    [],
                    tool_root=Path(record.root) if record.root else None,
                )
            ),
        )


@pytest.fixture
def terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TerminalDaemon]:
    """A terminal with an explicit factory that tests may replace before use."""
    del tmp_path
    with terminal_daemon(monkeypatch) as daemon:
        yield daemon


def session_for(backend: DaemonSessionBackend):
    return backend.session
