"""Real ASGI Streamable HTTP, health, and bearer protection tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import httpx2

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.auth import SingleUserOAuthProvider
from yoke.mcp_server.server import create_service

from .helpers import http_client


def test_health_is_public_but_mcp_requires_configured_bearer(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(root=tmp_path, bearer_token="test-secret")
        )
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                headers={"Host": "localhost"},
            ) as client:
                health = await client.get("/healthz")
                denied = await client.post("/mcp", json={})
                assert health.status_code == 200
                assert health.json() == {"ok": True}
                assert denied.status_code == 401
                assert denied.headers["www-authenticate"] == "Bearer"
            async with http_client(service, token="test-secret") as mcp:
                result = await mcp.list_tools()
                assert len(result.tools) == 10

    asyncio.run(scenario())


def test_transport_rejects_unapproved_host_header(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://attacker.example",
                headers={"Host": "attacker.example"},
            ) as client:
                response = await client.post("/mcp", json={})
                assert response.status_code == 421

    asyncio.run(scenario())


def test_cli_disables_access_logging(tmp_path: Path, monkeypatch) -> None:
    from yoke.mcp_server import cli

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "parse_config", lambda: MCPServerConfig(root=tmp_path))
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda _app, **kwargs: captured.update(kwargs)
    )

    cli.main()

    assert captured["access_log"] is False


def test_oauth_discovery_dcr_pkce_refresh_and_mcp_access(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(
                root=tmp_path,
                oauth_issuer_url="http://localhost",
                oauth_authorization_password="authorize-me",
                oauth_state_file=tmp_path / "oauth.json",
            )
        )
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                headers={"Host": "localhost"},
                follow_redirects=False,
            ) as client:
                metadata = await client.get("/.well-known/oauth-authorization-server")
                resource = await client.get("/.well-known/oauth-protected-resource/mcp")
                assert metadata.json()["registration_endpoint"].endswith("/register")
                assert resource.json()["resource"] == "http://localhost/mcp"

                registration = await client.post(
                    "/register",
                    json={
                        "client_name": "ChatGPT test",
                        "redirect_uris": ["http://localhost/callback"],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                    },
                )
                assert registration.status_code == 201
                assert registration.json()["scope"] == "yoke"
                client_id = registration.json()["client_id"]

                verifier = "v" * 64
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                authorization = await client.get(
                    "/authorize",
                    params={
                        "client_id": client_id,
                        "redirect_uri": "http://localhost/callback",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "scope": "yoke",
                        "state": "state-value",
                        "resource": "http://localhost/mcp",
                    },
                )
                consent_url = authorization.headers["location"]
                request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
                consent = await client.get(consent_url)
                assert "Authorize Yoke MCP" in consent.text

                rejected = await client.post(
                    "/oauth/consent",
                    data={"request_id": request_id, "password": "wrong"},
                )
                assert rejected.status_code == 401
                approved = await client.post(
                    "/oauth/consent",
                    data={"request_id": request_id, "password": "authorize-me"},
                )
                callback = approved.headers["location"]
                callback_query = parse_qs(urlparse(callback).query)
                assert callback_query["state"] == ["state-value"]

                token = await client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": callback_query["code"][0],
                        "code_verifier": verifier,
                        "redirect_uri": "http://localhost/callback",
                        "resource": "http://localhost/mcp",
                    },
                )
                assert token.status_code == 200
                tokens = token.json()

                refreshed = await client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": tokens["refresh_token"],
                        "scope": "yoke",
                    },
                )
                assert refreshed.status_code == 200
                assert refreshed.json()["access_token"] != tokens["access_token"]

                denied = await client.post("/mcp", json={})
                assert denied.status_code == 401
                assert "resource_metadata=" in denied.headers["www-authenticate"]

            async with http_client(
                service, token=refreshed.json()["access_token"]
            ) as mcp:
                result = await mcp.list_tools()
                assert len(result.tools) == 10

        state_file = tmp_path / "oauth.json"
        assert state_file.stat().st_mode & 0o777 == 0o600
        state_payload = json.loads(state_file.read_text())
        subjects = {
            value["subject"]
            for section in ("access_tokens", "refresh_tokens")
            for value in state_payload[section].values()
        }
        assert subjects == {"yoke-mcp-user"}

    asyncio.run(scenario())


def test_oauth_registration_rejects_unapproved_redirect_host(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(
                root=tmp_path,
                oauth_issuer_url="http://localhost",
                oauth_authorization_password="authorize-me",
                oauth_state_file=tmp_path / "oauth.json",
            )
        )
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                headers={"Host": "localhost"},
            ) as client:
                registration = await client.post(
                    "/register",
                    json={
                        "client_name": "Attacker",
                        "redirect_uris": ["https://attacker.example/callback"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registration.status_code == 400
                assert registration.json()["error"] == "invalid_redirect_uri"

    asyncio.run(scenario())


def test_oauth_state_migrates_legacy_subjects(tmp_path: Path) -> None:
    state_file = tmp_path / "oauth.json"
    provider = SingleUserOAuthProvider(
        issuer_url="http://localhost",
        resource_url="http://localhost/mcp",
        authorization_password="authorize-me",
        state_file=state_file,
    )
    token = provider._issue_tokens("client-id", ["yoke"])
    provider._save_state()

    payload = json.loads(state_file.read_text())
    for section in ("access_tokens", "refresh_tokens"):
        for value in payload[section].values():
            value["subject"] = "legacy-local-user"
    state_file.write_text(json.dumps(payload))

    SingleUserOAuthProvider(
        issuer_url="http://localhost",
        resource_url="http://localhost/mcp",
        authorization_password="authorize-me",
        state_file=state_file,
    )

    migrated = json.loads(state_file.read_text())
    subjects = {
        value["subject"]
        for section in ("access_tokens", "refresh_tokens")
        for value in migrated[section].values()
    }
    assert subjects == {"yoke-mcp-user"}
    assert token.access_token in migrated["access_tokens"]
