"""Isolated real-process daemon harness. Never uses the user's socket or port."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from yoke.daemon.client import connect_or_spawn, find_daemon, unix_client
from yoke.daemon.instance import daemon_alive, read_info
from yoke.daemon.paths import DaemonPaths

PLUGIN = """
import os
import time
import json
import sys
from pathlib import Path
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import ProviderModelInfo

PROVIDER_NAME = "fixture"
MODELS = (ProviderModelInfo(id="echo", display_name="Echo", context_window_tokens=32000),)

class Provider:
    supports_image_inputs = False
    def complete(self, messages, tools):
        text = next(m.plain_text_content for m in reversed(messages) if m.role == "user")
        if text == "background" and messages[-1].role == "user":
            code = "import os,time; from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"
            return Message(role="assistant", content=None, tool_calls=[ToolCall(
                id="background-call", function=ToolFunction(name="command_exec", arguments=json.dumps({
                    "argv": [sys.executable, "-c", code], "mode": "background"
                }))
            )])
        if text == "slow":
            Path(os.environ["YOKE_TEST_STARTED"]).touch()
            time.sleep(1.5)
        return Message.assistant("echo:" + text)
    def fork_for_turn(self):
        return Provider()

def register_provider(context):
    return Provider()

def list_provider_models(context):
    return list(MODELS)
"""


def eventually(probe: Callable[[], Any], timeout: float = 8) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError("Condition did not become true")


class DaemonSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = DaemonPaths.default()
        self.processes: list[subprocess.Popen[bytes]] = []

    def start(self, *args: str) -> int:
        self.paths.ensure()
        with self.paths.log.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "yoke", "serve", "--daemon", *args],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.processes.append(process)
        try:
            return eventually(lambda: self.status().get("pid"), timeout=8)
        except AssertionError:
            raise AssertionError(self.paths.log.read_text()) from None

    def status(self) -> dict[str, Any]:
        path = find_daemon(self.paths)
        if path is None:
            return {}
        with unix_client(path) as http:
            response = http.get("daemon")
            return response.json()["data"] if response.is_success else {}

    def connect(self):  # noqa: ANN201
        try:
            return connect_or_spawn(self.paths, timeout=15)
        except Exception as exc:
            raise AssertionError(self.paths.log.read_text()) from exc

    def wait_stopped(self, timeout: float = 8) -> None:
        eventually(lambda: not daemon_alive(self.paths), timeout)

    def stop(self) -> None:
        try:
            if find_daemon(self.paths):
                with unix_client(self.paths.socket) as http:
                    http.post("daemon/stop", json={"force": True})
            self.wait_stopped()
        except Exception:  # noqa: BLE001
            info = read_info(self.paths)
            if info and daemon_alive(self.paths):
                os.kill(info.pid, signal.SIGKILL)
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=8)


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[DaemonSandbox]:
    # The package-walk preload is proved by test_build and by child-driven
    # headless/autospawn races. Parent-driven lifecycle/auth daemons may import
    # their real runtime lazily instead of eagerly importing every provider.
    initialize = subprocess.Popen.__init__
    bootstrap = (
        "import runpy; import yoke.daemon.server as server; "
        "server.preload_runtime = lambda: None; "
        "runpy.run_module('yoke', run_name='__main__')"
    )

    def launch(process, args, *positional, **options):
        if isinstance(args, (list, tuple)) and "-m" in args:
            parts = list(args)
            index = parts.index("-m")
            if parts[index : index + 4] == ["-m", "yoke", "serve", "--daemon"]:
                parts[index : index + 2] = ["-c", bootstrap]
                args = parts
        initialize(process, args, *positional, **options)

    monkeypatch.setattr(subprocess.Popen, "__init__", launch)
    # Keep sun_path short even with pytest's descriptive test directory names.
    with TemporaryDirectory(prefix="ykd-") as directory:
        root = Path(directory)
        home = root / "home"
        providers = home / ".yoke" / "providers"
        providers.mkdir(parents=True)
        (providers / "fixture.py").write_text(PLUGIN)
        (home / ".yoke" / "config.json").write_text(
            '{"default_model":"fixture:echo", "capabilities":{"shell":"allow"}}'
        )
        for name, value in {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "YOKE_SESSION_DIR": str(root / "sessions"),
            "YOKE_DAEMON_PORT": "0",
            "YOKE_DAEMON_IDLE_GRACE": "0.4",
            "YOKE_DAEMON_BUILD_ID": "test-a",
            "YOKE_DAEMON_PROTOCOL": "1",
            "YOKE_TEST_STARTED": str(root / "started"),
        }.items():
            monkeypatch.setenv(name, value)
        for name in (
            "YOKE_HTTP_TOKEN",
            "YOKE_ACP_URL",
            "YOKE_ACP_TOKEN",
            "YOKE_DB_PATH",
        ):
            monkeypatch.delenv(name, raising=False)
        instance = DaemonSandbox(root)
        try:
            yield instance
        finally:
            instance.stop()
