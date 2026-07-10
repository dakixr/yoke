"""Provider login command tests."""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.ai.providers.codex.subscription import OAUTH_PROVIDER_ID
from yoke.ai.providers.codex.subscription import OAuthCredentials
from yoke.cli.main import app


def test_provider_login_stores_api_key_for_future_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.providers.app.Path.home", lambda: home)

    result = CliRunner().invoke(app, ["providers", "login", "zai"], input="secret\n")

    assert result.exit_code == 0
    credential_path = home / ".yoke" / "providers" / "credentials.json"
    assert json.loads(credential_path.read_text(encoding="utf-8")) == {
        "ZAI_API_KEY": "secret"
    }
    assert credential_path.stat().st_mode & 0o777 == 0o600


def test_provider_login_runs_codex_oauth_and_stores_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    credentials = OAuthCredentials(
        access="access",
        refresh="refresh",
        expires=123,
        account_id="account",
    )
    monkeypatch.setattr("yoke.cli.providers.app.Path.home", lambda: home)
    monkeypatch.setattr(
        "yoke.ai.providers.codex.subscription.login_openai_codex",
        lambda originator: credentials,
    )

    result = CliRunner().invoke(app, ["providers", "login", "codex"])

    assert result.exit_code == 0
    auth_path = home / ".codex" / "auth.json"
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    assert payload[OAUTH_PROVIDER_ID] == credentials.to_json()
    assert "Codex credentials saved" in result.stdout
