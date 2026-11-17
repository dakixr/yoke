"""Every connection edge and session-scoped recovery fence is explicit."""

from dataclasses import replace

import httpx
import pytest

from yoke.cli.backend.connection_state import (
    ConnectionPhase as P,
    ConnectionState,
    EDGES,
)


from .terminal_daemon_support import terminal as terminal


@pytest.mark.parametrize("before", list(P))
@pytest.mark.parametrize("after", list(P))
def test_connection_transition_table(before, after):
    state = ConnectionState("one")
    # Install each precondition directly so the test covers the full product.
    state._value = replace(state.snapshot, phase=before)
    legal = after == before or (
        before != P.CLOSED and (after == P.CLOSED or after in EDGES[before])
    )
    if legal:
        result = state.enter(after, cause="table")
        assert result.phase == after
        assert result.generation == (after == P.READY and before != after)
    else:
        with pytest.raises(ValueError, match="Illegal connection transition"):
            state.enter(after, cause="table")


def test_switch_atomically_resets_cursor_and_fences_recovery():
    state = ConnectionState("one")
    assert state.advance("one", 42)
    old = state.snapshot
    state.switch_session("two")
    assert state.snapshot.session_id == "two" and state.snapshot.seq == 0
    assert not state.advance("one", 43)
    with pytest.raises(RuntimeError, match="Session changed"):
        state.wait_ready(old.generation, epoch=old.epoch, timeout=0)
    state.enter(P.CLOSED, cause="done")
    with pytest.raises(RuntimeError, match="closed"):
        state.switch_session("three")


def test_get_does_not_replay_after_session_switch(terminal, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from yoke.cli.backend import connection as module

    backend = terminal.backend("one", root=tmp_path)
    connection = backend.connection
    calls = []

    @contextmanager
    def transport(*args, **kwargs):
        class Client:
            def request(self, method, route, **kwargs):
                calls.append(route)
                connection.state.switch_session("two")
                raise httpx.ConnectError("disconnected")

        yield Client()

    monkeypatch.setattr(module, "unix_client", transport)
    with pytest.raises(RuntimeError, match="Session changed"):
        connection.session_request("GET", "/queue")
    assert calls == ["session/one/queue"]
