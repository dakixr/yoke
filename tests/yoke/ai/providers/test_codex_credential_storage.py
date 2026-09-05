from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex.subscription import profiles
from yoke.ai.providers.codex.subscription.models import OAuthCredentials


def test_auth_refresh_failure_releases_lock_without_overwriting_credentials(
    tmp_path: Path,
) -> None:
    store = profiles.AuthStorage(tmp_path / "auth.json")
    expired = OAuthCredentials("old-access", "old-refresh", 0, "account")
    other = OAuthCredentials("other-access", "other-refresh", 0, "other-account")
    store.set_oauth("codex", expired)
    store.set_oauth("other", other)
    original = store.path.read_bytes()

    def failed_refresh(credentials: OAuthCredentials) -> OAuthCredentials:
        assert credentials == expired
        assert store.lock_path.exists()
        raise RuntimeError("refresh unavailable")

    with pytest.raises(RuntimeError, match="refresh unavailable"):
        store.refresh_oauth_with_lock("codex", failed_refresh)

    assert not store.lock_path.exists()
    assert store.path.read_bytes() == original
    refreshed = OAuthCredentials("new-access", "new-refresh", 10**15, "account")
    assert store.refresh_oauth_with_lock("codex", lambda _: refreshed) == refreshed
    assert json.loads(store.path.read_text()) == {
        "codex": refreshed.to_json(),
        "other": other.to_json(),
    }
    assert not store.lock_path.exists()


@pytest.mark.parametrize("label", ["auth", "Codex profile"])
def test_credential_lock_timeout_leaves_existing_lock_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    lock_path = tmp_path / "selection.lock"
    lock_path.write_text("another process")
    clock = iter((0.0, 31.0))
    monkeypatch.setattr(
        profiles, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )

    with pytest.raises(ProviderError) as caught:
        with profiles._credential_lock(lock_path, label=label):
            pytest.fail("An existing lock must not be acquired.")

    assert str(caught.value) == f"Timed out waiting for {label} lock {lock_path}."
    assert lock_path.read_text() == "another process"
