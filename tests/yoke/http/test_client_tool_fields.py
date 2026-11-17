"""Optional client tools and directories work for every attachment kind."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.yoke.http.test_client_session import ToolProvider, app_for, configure
from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session


@pytest.mark.parametrize("kind", ["cli", "web", "acp"])
def test_client_mcp_and_directories_reach_the_provider(tmp_path, kind):
    provider = ToolProvider("mcp_inspect", {})
    extra = tmp_path / "extra"
    extra.mkdir()
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        assert (
            configure(
                client,
                kind=kind,
                mcpServers=[{"name": "fixture", "command": "unused"}],
                additionalDirectories=[str(extra), str(extra) + "/../extra"],
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/session/session-a/prompt",
                headers=_auth(),
                json={"prompt": {"text": "inspect"}},
            ).status_code
            == 200
        )
        result = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 2000},
        )
        assert result.json()["data"]["state"] == "idle"
        assert {"mcp_inspect", "mcp_call"} <= provider.tools_seen[0]
        interactive = {"update_plan", "request_user_input"}
        if kind == "acp":
            assert interactive <= provider.tools_seen[0]
        else:
            assert not interactive & provider.tools_seen[0]
        assert "Additional workspace directories authorized by the client:\n" + str(
            extra
        ) in [
            message.plain_text_content
            for message in provider.messages_seen[0]
            if message.role == "system"
        ]
        tool = next(
            message for message in provider.messages_seen[-1] if message.role == "tool"
        )
        assert "fixture" in json.dumps(tool.model_dump())
