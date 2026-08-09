"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from dataclasses import dataclass

from .tokens import account_id_from_access_token


@dataclass(slots=True)
class OAuthCredentials:
    access: str
    refresh: str
    expires: int
    account_id: str

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> OAuthCredentials:
        access = payload.get("access")
        refresh = payload.get("refresh")
        expires = payload.get("expires")
        account_id = payload.get("accountId")
        if not isinstance(access, str) or not access:
            raise ValueError("Stored Codex auth is missing an access token.")
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("Stored Codex auth is missing a refresh token.")
        if not isinstance(expires, int | float):
            raise ValueError("Stored Codex auth is missing expiry metadata.")
        if not isinstance(account_id, str) or not account_id:
            account_id = account_id_from_access_token(access)
        return cls(
            access=access,
            refresh=refresh,
            expires=int(expires),
            account_id=account_id,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "type": "oauth",
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            "accountId": self.account_id,
        }
