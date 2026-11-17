"""Unit checks for peer credentials, skew decisions and shutdown races."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from yoke.daemon.client import skew_action
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.daemon.peer import connection_authorized, peer_authorized, peer_uid


def test_peer_check_rejects_other_and_unknown_uids():
    assert peer_authorized(123, owner_uid=123)
    assert not peer_authorized(456, owner_uid=123)
    assert not peer_authorized(None, owner_uid=123)


def test_kernel_peer_uid_is_current_user():
    left, right = socket.socketpair()
    try:
        assert peer_uid(left) == os.getuid()
        assert connection_authorized(left)
    finally:
        left.close()
        right.close()


def test_peer_uid_without_so_peercred_uses_getpeereid(monkeypatch):
    # macOS has no SO_PEERCRED; the owner must still be admitted there.
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    monkeypatch.setattr("yoke.daemon.peer._getpeereid_uid", lambda _fd: os.getuid())
    left, right = socket.socketpair()
    try:
        assert peer_uid(left) == os.getuid()
        assert connection_authorized(left)
    finally:
        left.close()
        right.close()


def test_real_getpeereid_reports_current_user_where_libc_has_it():
    import ctypes.util

    from yoke.daemon.peer import _getpeereid_uid

    libc = ctypes.util.find_library("c")
    if libc is None or not hasattr(ctypes.CDLL(libc), "getpeereid"):
        pytest.skip("libc has no getpeereid")
    left, right = socket.socketpair()
    try:
        assert _getpeereid_uid(left.fileno()) == os.getuid()
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize(
    "busy,protocol,expected",
    [
        (False, "1", "restart"),
        (False, "2", "restart"),
        (True, "1", "warn"),
        (True, "2", "refuse"),
    ],
)
def test_skew_policy(busy, protocol, expected):
    assert (
        skew_action(
            client_build="3.35.0+gnew",
            client_protocol=protocol,
            daemon_build="3.34.1+gold",
            daemon_protocol="1",
            busy=busy,
        )
        == expected
    )


def test_same_build_with_incompatible_protocol_still_refuses():
    assert (
        skew_action(
            client_build="same",
            client_protocol="2",
            daemon_build="same",
            daemon_protocol="1",
            busy=True,
        )
        == "refuse"
    )


def test_restart_waits_for_mutation_that_races_the_busy_probe():
    async def scenario():
        lifecycle = DaemonLifecycle(poll_seconds=0.01)
        entered = asyncio.Event()

        async def probe():
            if not entered.is_set():
                lifecycle.begin_mutation()
                entered.set()
            return False

        lifecycle.bind_busy_probe(probe)
        lifecycle.request_restart_when_idle()
        watcher = asyncio.create_task(lifecycle.watch())
        try:
            await entered.wait()
            await asyncio.sleep(0.03)
            assert lifecycle.exit_reason is None
            lifecycle.end_mutation()
            await asyncio.wait_for(watcher, 1)
            assert lifecycle.exit_reason == "restart"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


def test_idle_timer_resets_when_a_new_lease_arrives():
    async def scenario():
        lifecycle = DaemonLifecycle(grace_seconds=0.12, poll_seconds=0.01)
        watcher = asyncio.create_task(lifecycle.watch())
        try:
            await asyncio.sleep(0.08)
            lease = lifecycle.open_lease("test")
            await asyncio.sleep(0.08)
            lifecycle.close_lease(lease)
            await asyncio.sleep(0.08)
            assert lifecycle.exit_reason is None
            await asyncio.wait_for(watcher, 1)
            assert lifecycle.exit_reason == "idle"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


def test_busy_tcp_port_keeps_unix_listener_available(short_socket_dir):
    from yoke.daemon.server import _bind_tcp, _bind_unix

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        listener = _bind_unix(short_socket_dir / "daemon.sock")
        try:
            assert _bind_tcp(occupied.getsockname()[1]) is None
            assert listener.family == socket.AF_UNIX
            assert listener.fileno() >= 0
            assert (short_socket_dir / "daemon.sock").exists()
        finally:
            listener.close()


def test_daemon_without_idle_exit_probes_only_for_a_pending_restart():
    async def scenario():
        lifecycle = DaemonLifecycle(idle_exit=False, poll_seconds=0.01)
        calls = 0

        async def probe():
            nonlocal calls
            calls += 1
            return False

        lifecycle.bind_busy_probe(probe)
        watcher = asyncio.create_task(lifecycle.watch())
        try:
            await asyncio.sleep(0.1)
            assert calls == 0
            lifecycle.request_restart_when_idle()
            await asyncio.wait_for(watcher, 1)
            assert calls >= 1
            assert lifecycle.exit_reason == "restart"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


def test_queued_prompt_presence_matches_stored_queues(tmp_path):
    from yoke.session import SessionStore
    from yoke.session.queue import (
        PersistedPendingInput,
        PersistedPromptQueue,
        has_queued_prompts,
        write_prompt_queue_snapshot,
    )

    store = SessionStore(tmp_path / "sessions")
    store.save("saved", [], root=tmp_path)
    assert not has_queued_prompts(store.directory)
    paused = PersistedPendingInput(
        id="input", prompt="later", created_at="now", paused=True
    )
    write_prompt_queue_snapshot(
        store.directory, "saved", PersistedPromptQueue(prompts=[paused])
    )
    assert not has_queued_prompts(store.directory)
    runnable = paused.model_copy(update={"id": "runnable", "paused": False})
    write_prompt_queue_snapshot(
        store.directory, "saved", PersistedPromptQueue(prompts=[paused, runnable])
    )
    assert has_queued_prompts(store.directory)
    write_prompt_queue_snapshot(store.directory, "saved", PersistedPromptQueue())
    assert not has_queued_prompts(store.directory)
