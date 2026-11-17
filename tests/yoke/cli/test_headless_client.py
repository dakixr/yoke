"""User-facing headless entry points exercise isolated detached daemons."""

import subprocess
import sys

from tests.yoke.daemon.support import sandbox as sandbox


def invoke(root, *args):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            # Inject only the existing startup deadline. Entry point, daemon
            # spawn, transport, rendering and persistence stay real.
            "import runpy; from yoke.daemon.client import connect_or_spawn; "
            "connect_or_spawn.__kwdefaults__['timeout'] = 15; "
            "runpy.run_module('yoke', run_name='__main__')",
            *args,
        ],
        cwd=root,
        input="",
        capture_output=True,
        text=True,
        timeout=25,
    )


def test_headless_autospawns_and_prints(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DAEMON_PROTOCOL", "3")
    monkeypatch.setenv("YOKE_DB_PATH", str(sandbox.root / "db.sqlite"))
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "10")
    result = invoke(sandbox.root, "--headless", "--session", "saved", "hello")
    assert (result.stdout, result.stderr, result.returncode) == ("echo:hello\n", "", 0)
    assert sandbox.status()["pid"]
    from yoke.session import SessionStore

    store = SessionStore()
    try:
        assert [message.text_content() for message in store.load("saved").messages] == [
            "hello",
            "echo:hello",
        ]
    finally:
        store.database.close()
