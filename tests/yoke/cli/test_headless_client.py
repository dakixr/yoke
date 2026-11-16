"""User-facing headless entry points exercise isolated detached daemons."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from tests.yoke.daemon.support import sandbox as sandbox


def invoke(root, *args):
    return subprocess.run(
        [sys.executable, "-m", "yoke", *args],
        cwd=root,
        input="",
        capture_output=True,
        text=True,
        timeout=25,
    )


def test_headless_autospawns_prints_and_resumes(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DB_PATH", str(sandbox.root / "db.sqlite"))
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    result = invoke(sandbox.root, "--headless", "--session", "saved", "hello")
    assert (result.stdout, result.stderr, result.returncode) == ("echo:hello\n", "", 0)
    pid = sandbox.status()["pid"]
    resumed = invoke(sandbox.root, "resume", "saved", "--headless", "--prompt", "again")
    assert (resumed.stdout, resumed.stderr, resumed.returncode) == (
        "echo:again\n",
        "",
        0,
    )
    assert sandbox.status()["pid"] == pid
    sandbox.wait_stopped(timeout=8)


def test_two_print_clients_share_one_daemon(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DB_PATH", str(sandbox.root / "db.sqlite"))
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda text: invoke(sandbox.root, "-p", text), ["one", "two"])
        )
    assert [
        (result.stdout, result.stderr, result.returncode) for result in results
    ] == [("echo:one\n", "", 0), ("echo:two\n", "", 0)]
    assert sandbox.status()["pid"]
    assert sandbox.paths.log.read_text().count("Yoke daemon listening") == 1
    sandbox.wait_stopped(timeout=8)


def test_resume_short_prompt_alias(sandbox):
    first = invoke(sandbox.root, "--headless", "--session", "saved", "first")
    assert (first.returncode, first.stdout, first.stderr) == (0, "echo:first\n", "")
    result = invoke(sandbox.root, "resume", "saved", "-p", "short alias")
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "echo:short alias\n",
        "",
    )


def test_resume_relocates_through_daemon_in_interactive_and_headless_modes(
    sandbox, monkeypatch
):
    from yoke.daemon.client import unix_client

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "10")
    assert (
        invoke(sandbox.root, "--headless", "--session", "saved", "first").returncode
        == 0
    )
    pid = sandbox.status()["pid"]
    for index, options in enumerate(((), ("-p", "after relocation"))):
        destination = sandbox.root / f"relocated-{index}"
        destination.mkdir()
        result = invoke(
            sandbox.root, "resume", "saved", "--relocate", str(destination), *options
        )
        assert result.returncode == 0, result.stderr
        assert sandbox.status()["pid"] == pid
        with unix_client(sandbox.paths.socket) as http:
            record = http.get("session/saved").raise_for_status().json()["data"]
        assert record["location"]["directory"] == str(destination)
