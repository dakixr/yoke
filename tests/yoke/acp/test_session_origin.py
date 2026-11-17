"""ACP creation identifies its origin even without client configuration support."""

import asyncio
from pathlib import Path

from tests.yoke.acp.test_bridge import FixtureNative
from yoke.acp.bridge import YokeAcpAgent


def test_acp_without_client_configuration_still_records_origin(tmp_path: Path):
    async def scenario():
        native = FixtureNative()
        agent = YokeAcpAgent(native)
        try:
            await agent.new_session(str(tmp_path))
            assert native.session_create is not None
            assert native.session_create["origin"] == "acp"
            assert "client" not in native.session_create
        finally:
            await agent.close()

    asyncio.run(scenario())
