"""Regression checks for daemon admission and private filesystem boundaries."""

from __future__ import annotations

import asyncio

from yoke.daemon.lifecycle import DaemonLifecycle


def test_existing_daemon_directories_become_private(sandbox):
    paths = sandbox.paths
    for directory in (paths.state_dir, paths.run_dir, paths.log.parent):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o777)
    paths.ensure()
    for directory in (paths.state_dir, paths.run_dir, paths.log.parent):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_restart_waits_for_admission_completed_during_busy_probe():
    async def scenario():
        lifecycle = DaemonLifecycle(poll_seconds=0.01)
        calls = 0

        async def probe():
            nonlocal calls
            calls += 1
            if calls == 1:
                # The work probe already scanned runtimes, then yielded to its
                # queue read. A prompt handler admits work and returns meanwhile.
                lifecycle.begin_mutation()
                lifecycle.end_mutation()
                return False
            return True

        lifecycle.bind_busy_probe(probe)
        lifecycle.request_restart_when_idle()
        watcher = asyncio.create_task(lifecycle.watch())
        try:
            await asyncio.sleep(0.05)
            assert lifecycle.exit_reason is None
            assert calls >= 2
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


def test_nonforced_stop_rechecks_admissions_before_exiting():
    from types import SimpleNamespace

    from starlette.requests import Request

    from yoke.http.routes.daemon import daemon_stop

    async def scenario():
        lifecycle = DaemonLifecycle(poll_seconds=0.01)
        working = False

        async def probe():
            return working

        lifecycle.bind_busy_probe(probe)
        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(state=SimpleNamespace(daemon=lifecycle)),
            }
        )
        await daemon_stop(request)
        # Work arrives in the response-to-shutdown window.
        working = True
        watcher = asyncio.create_task(lifecycle.watch())
        try:
            await asyncio.sleep(0.08)
            assert lifecycle.exit_reason is None
            working = False
            await asyncio.wait_for(watcher, 1)
            assert lifecycle.exit_reason == "stop"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


def test_unusable_socket_path_has_actionable_discovery_error(sandbox, monkeypatch):
    import pytest

    from yoke.daemon.client import DaemonUnavailable, connect_or_spawn

    home = sandbox.root / ("h" * 110)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "runtime"))
    with pytest.raises(DaemonUnavailable, match="socket path is too long"):
        connect_or_spawn()


def test_detached_logs_rotate_large_existing_log(sandbox):
    sandbox.paths.ensure()
    sandbox.paths.log.write_bytes(b"old log\n" * (400 * 1024))
    from yoke.daemon.log import detached_stdio

    with detached_stdio(sandbox.paths.log):
        print("new log entry")
    assert sandbox.paths.log.stat().st_size < 2 * 1024 * 1024
    assert sandbox.paths.log.with_name("daemon.log.1").exists()


def test_detached_log_rotates_during_one_run(sandbox):
    from yoke.daemon.log import MAX_LOG_BYTES, detached_stdio

    sandbox.paths.ensure()
    with detached_stdio(sandbox.paths.log):
        print("x" * (MAX_LOG_BYTES * 4))
    files = list(sandbox.paths.log.parent.glob("daemon.log*"))
    assert len(files) == 3
    assert all(path.stat().st_size <= MAX_LOG_BYTES for path in files)
