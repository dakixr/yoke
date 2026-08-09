"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from yoke.ai.providers.base import (
    ProviderError,
)
from yoke.ai.providers.storage import write_private_json

from .models import OAuthCredentials
from .oauth import (
    account_id_from_access_token,
    login_openai_codex,
    refresh_openai_codex_token,
)
from .tokens import jwt_exp_millis as _jwt_exp_millis
from .tokens import required_str as _required_str
from .tokens import utc_now_iso as _utc_now_iso


@dataclass(slots=True)
class CodexProfile:
    name: str
    payload: dict[str, Any]

    def credentials(self) -> OAuthCredentials:
        tokens = self._tokens()
        access = _required_str(tokens, "access_token", self.name)
        refresh = _required_str(tokens, "refresh_token", self.name)
        account_id = tokens.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            account_id = account_id_from_access_token(access)
        expires = _jwt_exp_millis(access)
        return OAuthCredentials(
            access=access,
            refresh=refresh,
            expires=expires,
            account_id=account_id,
        )

    def with_credentials(self, credentials: OAuthCredentials) -> dict[str, Any]:
        updated = dict(self.payload)
        tokens = dict(self._tokens())
        tokens["access_token"] = credentials.access
        tokens["refresh_token"] = credentials.refresh
        tokens["account_id"] = credentials.account_id
        updated["tokens"] = tokens
        updated["last_refresh"] = _utc_now_iso()
        return updated

    def _tokens(self) -> dict[str, Any]:
        tokens = self.payload.get("tokens")
        if not isinstance(tokens, dict):
            raise ProviderError(f"Codex profile {self.name!r} is missing tokens.")
        return tokens


@dataclass(slots=True)
class QuotaWindow:
    used_percent: int | None
    resets_at: int | None
    duration_mins: int | None


@dataclass(slots=True)
class QuotaLimit:
    primary: QuotaWindow | None
    secondary: QuotaWindow | None


@dataclass(slots=True)
class QuotaSnapshot:
    default_limit: QuotaLimit | None
    updated_auth: dict[str, Any]


@dataclass(slots=True)
class Pace:
    delta_percent: float
    resets_in_seconds: float


@dataclass(slots=True)
class AccountScore:
    score: float
    rejected: bool


class CodexProfileStore:
    def __init__(
        self,
        accounts_dir: Path,
        auths_path: Path,
        selection_path: Path,
        ttl_seconds: int,
    ) -> None:
        self.accounts_dir = accounts_dir.expanduser().resolve()
        self.auths_path = auths_path.expanduser().resolve()
        self.selection_path = selection_path.expanduser().resolve()
        self.ttl_seconds = ttl_seconds
        self.lock_path = self.selection_path.with_suffix(
            self.selection_path.suffix + ".lock"
        )

    def fresh_credentials(self) -> OAuthCredentials:
        credentials, _profile_name = self.fresh_credentials_with_profile()
        return credentials

    def fresh_credentials_with_profile(self) -> tuple[OAuthCredentials, str]:
        with self._lock():
            profiles = self._read_profiles()
            profile = self._cached_profile(profiles)
            if profile is None:
                profile = self._select_best_profile(profiles)
                self._write_selection(profile.name)
            credentials = profile.credentials()
            if credentials.expires - int(time.time() * 1000) > 60_000:
                return credentials, profile.name
            refreshed = refresh_openai_codex_token(credentials)
            self._write_profile(profile.name, profile.with_credentials(refreshed))
            return refreshed, profile.name

    def _cached_profile(self, profiles: dict[str, CodexProfile]) -> CodexProfile | None:
        selection = self._read_selection()
        name = selection.get("selected_profile")
        selected_at = selection.get("selected_at")
        if not isinstance(name, str) or not isinstance(selected_at, int | float):
            return None
        if time.time() - float(selected_at) > self.ttl_seconds:
            return None
        return profiles.get(name)

    def _select_best_profile(self, profiles: dict[str, CodexProfile]) -> CodexProfile:
        from .quota import query_codex_quota, score_quota_snapshot

        best_profile: CodexProfile | None = None
        best_score = float("inf")
        failures: list[str] = []
        for profile in profiles.values():
            try:
                snapshot = query_codex_quota(profile.payload)
                self._write_profile(profile.name, snapshot.updated_auth)
                account_score = score_quota_snapshot(snapshot)
            except Exception as exc:
                failures.append(f"{profile.name}: {exc}")
                continue
            if account_score.rejected:
                continue
            if account_score.score < best_score:
                best_profile = CodexProfile(profile.name, snapshot.updated_auth)
                best_score = account_score.score
        if best_profile is not None:
            return best_profile
        cached_name = self._read_selection().get("selected_profile")
        if isinstance(cached_name, str) and cached_name in profiles:
            return profiles[cached_name]
        fallback_profile = self._first_locally_usable_profile(profiles)
        if fallback_profile is not None:
            return fallback_profile
        details = "; ".join(failures) if failures else "no profiles configured"
        raise ProviderError(f"No usable Codex profile found: {details}")

    def _first_locally_usable_profile(
        self, profiles: dict[str, CodexProfile]
    ) -> CodexProfile | None:
        for profile in profiles.values():
            try:
                credentials = profile.credentials()
            except Exception:
                continue
            if credentials.expires - int(time.time() * 1000) > 60_000:
                return profile
        return None

    def _read_profiles(self) -> dict[str, CodexProfile]:
        return self._read_account_profiles()

    def _read_account_profiles(self) -> dict[str, CodexProfile]:
        profiles: dict[str, CodexProfile] = {}
        if not self.accounts_dir.exists():
            return profiles
        for path in sorted(self.accounts_dir.glob("*/auth.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"Unable to parse Codex auth profile {path}."
                ) from exc
            if not isinstance(payload, dict):
                raise ProviderError(f"Codex auth profile {path} is invalid.")
            profiles[path.parent.name] = CodexProfile(path.parent.name, payload)
        return profiles

    def _write_profile(self, name: str, payload: dict[str, Any]) -> None:
        account_path = self.accounts_dir / name / "auth.json"
        if account_path.exists() or self.accounts_dir.exists():
            self._atomic_write(account_path, payload)
            return
        profiles = json.loads(self.auths_path.read_text(encoding="utf-8"))
        if not isinstance(profiles, dict):
            raise ProviderError(
                f"Codex auth profiles file {self.auths_path} is invalid."
            )
        profiles[name] = payload
        self._atomic_write(self.auths_path, profiles)

    def _read_selection(self) -> dict[str, Any]:
        if not self.selection_path.exists():
            return {}
        try:
            payload = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_selection(self, profile_name: str) -> None:
        self._atomic_write(
            self.selection_path,
            {"selected_profile": profile_name, "selected_at": time.time()},
        )

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        write_private_json(path, payload)

    @contextlib.contextmanager
    def _lock(self) -> Any:
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30
        handle: int | None = None
        while handle is None:
            try:
                handle = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"Timed out waiting for Codex profile lock {self.lock_path}."
                    ) from None
                time.sleep(0.1)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()


class AuthStorage:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def get_oauth(self, provider_id: str) -> OAuthCredentials | None:
        payload = self._read()
        raw = payload.get(provider_id)
        if not isinstance(raw, dict):
            return None
        if raw.get("type") != "oauth":
            return None
        return OAuthCredentials.from_json(raw)  # ty:ignore[invalid-argument-type]

    def set_oauth(self, provider_id: str, credentials: OAuthCredentials) -> None:
        with self._lock():
            payload = self._read()
            payload[provider_id] = credentials.to_json()
            self._write(payload)

    def refresh_oauth_with_lock(
        self,
        provider_id: str,
        refresher: Callable[[OAuthCredentials], OAuthCredentials],
    ) -> OAuthCredentials:
        with self._lock():
            current = self.get_oauth(provider_id)
            if current is None:
                current = login_openai_codex("yoke")
                self._write_provider(provider_id, current)
                return current
            if current.expires - int(time.time() * 1000) > 60_000:
                return current
            refreshed = refresher(current)
            self._write_provider(provider_id, refreshed)
            return refreshed

    def _write_provider(self, provider_id: str, credentials: OAuthCredentials) -> None:
        payload = self._read()
        payload[provider_id] = credentials.to_json()
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Unable to parse Codex auth file {self.path}."
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Codex auth file {self.path} is invalid.")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        write_private_json(self.path, payload)

    @contextlib.contextmanager
    def _lock(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30
        handle: int | None = None
        while handle is None:
            try:
                handle = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"Timed out waiting for auth lock {self.lock_path}."
                    ) from None
                time.sleep(0.1)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()
