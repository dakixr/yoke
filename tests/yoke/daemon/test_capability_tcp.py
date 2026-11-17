"""Loopback reporting and execution retain per-run capability authentication."""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.protocol import RegistrationError, request
from yoke.agent_runs.records import Owner
from yoke.mcp_server.execution import bridge, client
from yoke.mcp_server.results.store import ResultStore


def test_run_reporting_tcp_requires_issued_capability(monkeypatch):
    # Exercise the actual Windows fallback on Linux as well.
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    registry = AgentRunRegistry()
    try:
        address = registry.address()
        assert address.startswith("tcp:")
        frame: dict[str, object] = {
            "op": "register",
            "runId": "run",
            "agentId": "agent",
            "name": "test",
            "provider": "fixture",
            "model": "echo",
        }
        with pytest.raises(RegistrationError):
            request(address, frame)
        with pytest.raises(RegistrationError):
            request(address, {**frame, "token": "wrong"})
        assert not registry.snapshots()
        capability = registry.capability(Owner(session_id="owner"))
        result = request(address, {**frame, "token": capability})
        assert result["token"]
        assert registry.snapshots()[0]["runId"] == "run"
    finally:
        registry.close()


def test_execution_tcp_authenticates_and_revokes_capabilities(monkeypatch):
    monkeypatch.setattr(bridge, "sys", SimpleNamespace(platform="win32"))

    async def scenario():
        async def dispatch(*args, **kwargs):
            raise AssertionError("Invalid capabilities must not dispatch")

        host = bridge.PythonBridge(dispatch, ResultStore(), ())
        try:
            token, _, _ = await host.prepare(bridge.ComposePython(code="pass"))
            assert host.address.startswith("tcp:")
            client.configure(host.address, "wrong")
            with pytest.raises(RuntimeError, match="Unknown, cancelled or expired"):
                await asyncio.to_thread(client._request, "emit", {"data": "blocked"})
            client.configure(host.address, token)
            result = await asyncio.to_thread(client._request, "emit", {"data": "hello"})
            assert result["ok"]
            host.revoke(token)
            with pytest.raises(RuntimeError, match="Unknown, cancelled or expired"):
                await asyncio.to_thread(client._request, "emit", {"data": "revoked"})
            # Missing tokens fail before dispatch too.
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", int(host.address[4:])
            )
            writer.write(b'{"operation":"call","arguments":{}}\n')
            await writer.drain()
            assert "bridge_error" in json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
        finally:
            await host.close()
            client.configure("", "")

    asyncio.run(scenario())
