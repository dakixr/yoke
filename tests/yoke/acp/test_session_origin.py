"""ACP creation identifies its origin and attaches its client configuration."""

import asyncio
from pathlib import Path

from tests.yoke.acp.test_bridge import FixtureNative
from yoke.acp.bridge import YokeAcpAgent


def test_acp_creation_records_origin_and_client(tmp_path: Path):
    async def scenario():
        native = FixtureNative()
        agent = YokeAcpAgent(native)
        try:
            await agent.new_session(str(tmp_path))
            assert native.session_create is not None
            assert native.session_create["origin"] == "acp"
            assert native.session_create["client"]["kind"] == "acp"
        finally:
            await agent.close()

    asyncio.run(scenario())
