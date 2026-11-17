"""Separate HTTP applications never share secrets or storage."""

from pathlib import Path

from fastapi.testclient import TestClient

from yoke.http.app import HttpAppSettings, create_app


def test_http_fixture_applications_keep_auth_and_storage_independent(tmp_path: Path):
    first = create_app(
        HttpAppSettings(auth_token="first", session_directory=tmp_path / "a")
    )
    second = create_app(
        HttpAppSettings(auth_token="second", session_directory=tmp_path / "b")
    )
    first.state.session_service.store.save("first-session", [], root=tmp_path)
    second.state.session_service.store.save("second-session", [], root=tmp_path)
    with TestClient(first) as a, TestClient(second) as b:
        for client, token, session, foreign in (
            (a, "first", "first-session", "second"),
            (b, "second", "second-session", "first"),
        ):
            assert client.get("/api/v1/session").status_code == 401
            assert (
                client.get(
                    "/api/v1/session", headers={"Authorization": f"Bearer {foreign}"}
                ).status_code
                == 401
            )
            response = client.get(
                "/api/v1/session", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
            assert [row["id"] for row in response.json()["data"]] == [session]
