"""A missing input contract ends attachment with a useful error, without retry."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from yoke.cli.backend import connection as module
from yoke.daemon.client import DaemonHandle, DaemonIncompatible
from yoke.http.models.capabilities import FeatureFlags


@pytest.mark.parametrize("protocol", ["2", "3"])
def test_incompatible_attachment_stops_the_connection(monkeypatch, protocol):
    features = FeatureFlags().model_dump(by_alias=True)
    features.pop("inputLifecycle")
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"data": {"protocolVersion": protocol, "features": features}}
        )

    @asynccontextmanager
    async def transport(*args, **kwargs):
        async with httpx.AsyncClient(
            base_url="http://fixture/", transport=httpx.MockTransport(respond)
        ) as http:
            yield http

    monkeypatch.setattr(module, "async_client", transport)
    discover = Mock(return_value=DaemonHandle(Path("fixture.sock"), 123, "4.0.0", "3"))
    connection = module.SessionConnection("session", discover=discover)
    try:
        with pytest.raises(DaemonIncompatible, match="inputLifecycle.*Upgrade"):
            connection.start(timeout=2)
        assert connection.state.closed
        assert discover.call_count == 2  # construction and the single stream attempt
        assert calls == [
            ("GET", "/capabilities"),
            ("DELETE", "/session/session/client"),
        ]
    finally:
        connection.close()
