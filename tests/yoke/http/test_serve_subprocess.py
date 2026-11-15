"""Smoke test of a real ``yoke serve`` process with a plugin provider.

The provider is a global provider plugin (``$HOME/.yoke/providers/*.py``), the
same mechanism users have for custom providers, so no source hooks are needed.
"""

# ruff: noqa: D103, S101, S603
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

TOKEN = "serve-smoke-token"
PLUGIN = """
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderModelInfo

PROVIDER_NAME = "smoke"
MODELS = (
    ProviderModelInfo(id="echo", display_name="Echo", context_window_tokens=32000),
)


class EchoProvider:
    supports_image_inputs = False

    def complete(self, messages, tools):
        text = next(
            m.plain_text_content for m in reversed(messages) if m.role == "user"
        )
        return Message.assistant(f"echo:{text}")

    def fork_for_turn(self):
        return EchoProvider()


def register_provider(context):
    return EchoProvider()


def list_provider_models(context):
    return list(MODELS)
"""


class Daemon:
    def __init__(self, tmp_path: Path) -> None:
        home = tmp_path / "serve-home"
        (home / ".yoke" / "providers").mkdir(parents=True)
        (home / ".yoke" / "providers" / "smoke.py").write_text(PLUGIN, encoding="utf-8")
        self.sessions = tmp_path / "serve-sessions"
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONUNBUFFERED": "1",
        }
        env.pop("YOKE_HTTP_TOKEN", None)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "yoke",
                "serve",
                "--port",
                "0",
                "--token",
                TOKEN,
                "--hide-token",
                "--session-dir",
                str(self.sessions),
            ],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.stderr: list[str] = []
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()
        self.port = self._read_port()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr.extend(self.process.stderr)

    def _read_port(self) -> int:
        assert self.process.stdout is not None
        result: list[int] = []

        def read() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                match = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
                if match:
                    result.append(int(match.group(1)))
                    return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(15)
        assert result, f"daemon did not report its port: {''.join(self.stderr)}"
        return result[0]

    def group_members(self) -> list[int]:
        pids = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.getpgid(int(entry.name)) == self.process.pid:
                    pids.append(int(entry.name))
            except ProcessLookupError:
                continue
        return pids

    def kill(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(5)


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    if not Path("/proc").is_dir():
        pytest.skip("process-group inspection needs /proc")
    started = Daemon(tmp_path)
    try:
        yield started
    finally:
        started.kill()


@pytest.mark.parametrize(
    "stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"]
)
def test_yoke_serve_runs_a_plugin_turn_and_exits_cleanly(
    daemon: Daemon, tmp_path: Path, stop_signal: signal.Signals
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{daemon.port}/api/v1",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    ) as http:
        assert (
            http.get("/session", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        created = http.post(
            "/session",
            json={
                "id": "smoke",
                "title": "Smoke",
                "location": {"directory": str(root)},
                "selection": {"provider": "smoke", "model": "echo"},
            },
        )
        assert created.status_code == 200, created.text
        admitted = http.post(
            "/session/smoke/prompt", json={"prompt": {"text": "hello daemon"}}
        )
        assert admitted.status_code == 200, admitted.text
        waited = http.post("/session/smoke/wait", params={"timeoutMs": 10000})
        assert waited.json()["data"]["state"] == "idle", waited.text
        messages = http.get("/session/smoke/message", params={"order": "asc"}).json()[
            "data"
        ]
        input_id = admitted.json()["data"]["id"]

    assert [(m["type"], m["content"][0]["text"]) for m in messages] == [
        ("user", "hello daemon"),
        ("assistant", "echo:hello daemon"),
    ]
    assert messages[0]["inputID"] == input_id
    # The turn is durable on disk, not only in the daemon's memory.
    assert any(daemon.sessions.rglob("*")), "nothing persisted in the session dir"

    members = daemon.group_members()
    assert daemon.process.pid in members
    daemon.process.send_signal(stop_signal)
    # uvicorn shuts down gracefully, then re-raises the signal it captured, so
    # "terminated by N" (-N, or 128+N via KeyboardInterrupt) is a clean exit.
    assert daemon.process.wait(10) in {0, -stop_signal, 128 + stop_signal}
    daemon._stderr_reader.join(5)
    stderr = "".join(daemon.stderr)
    assert "Traceback" not in stderr, stderr
    # No worker or tool process outlives the daemon.
    assert daemon.group_members() == []
