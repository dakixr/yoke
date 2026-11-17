"""Additive search responses and creating-client attribution on protocol 2."""

from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.agent.models import Message
from yoke.http.app import HttpAppSettings, create_app


def test_search_wire_paging_filters_and_creation_origins(tmp_path: Path):
    app = create_app(
        HttpAppSettings(auth_token="secret", session_directory=tmp_path / "db")
    )
    with TestClient(app, headers={"Authorization": "Bearer secret"}) as client:
        assert client.get("/api/v1/health").json()["data"]["protocolVersion"] == "2"
        for sid, extra, expected in [
            ("web", {}, "web"),
            ("cli", {"client": {"owner": "terminal", "kind": "cli"}}, "cli"),
            (
                "headless",
                {"headless": True, "client": {"owner": "headless", "kind": "cli"}},
                "cli",
            ),
            ("acp", {"origin": "acp"}, "acp"),
            ("sdk", {"origin": "sdk"}, "sdk"),
        ]:
            response = client.post(
                "/api/v1/session",
                json={"id": sid, "location": {"directory": str(tmp_path)}, **extra},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["origin"] == expected
        store = cast(FastAPI, client.app).state.session_service.store
        store.save(
            "first",
            [Message.user("walrus answer")],
            title="walrus",
            origin="unknown",
            root=tmp_path,
        )
        store.save(
            "second", [Message.assistant("walrus reply")], root=tmp_path, origin="web"
        )
        store.save("archived", [Message.user("walrus")], root=tmp_path)
        store.set_archived("archived", True)
        store.save("outside", [Message.user("walrus")], root=tmp_path / "outside")
        params = {
            "search": "walrus",
            "directory": str(tmp_path),
            "archived": False,
            "limit": 1,
        }
        first = client.get("/api/v1/session", params=params).json()
        assert first["total"] == 2
        assert first["data"][0]["id"] == "first"
        assert "<mark>walrus</mark>" in first["data"][0]["searchSnippet"]
        second = client.get(
            "/api/v1/session", params={**params, "cursor": first["cursor"]["next"]}
        ).json()
        assert [row["id"] for row in second["data"]] == ["second"]
        assert second["cursor"]["next"] is None
        filtered = client.get(
            "/api/v1/session", params={**params, "origin": "unknown"}
        ).json()
        assert filtered["total"] == 1
        changed_query = client.get(
            "/api/v1/session",
            params={**params, "origin": "unknown", "cursor": first["cursor"]["next"]},
        )
        assert changed_query.status_code == 400
        assert (
            client.get("/api/v1/session", params={"origin": "bogus"}).status_code == 422
        )
        for query in ['"', "*", 'walrus"*', '" OR NEAR(']:
            assert (
                client.get("/api/v1/session", params={"search": query}).status_code
                == 200
            )
