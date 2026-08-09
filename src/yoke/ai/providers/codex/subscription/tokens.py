"""JWT and persisted-token helpers for Codex authentication."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from yoke.ai.providers.base import ProviderError

from .catalog import JWT_CLAIM_PATH


def account_id_from_access_token(access_token: str) -> str:
    try:
        payload_segment = access_token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Unable to decode Codex access token.") from exc
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    raise ProviderError("Codex access token does not include a ChatGPT account id.")


def required_str(payload: dict[str, Any], key: str, profile_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError(f"Codex profile {profile_name!r} is missing tokens.{key}.")
    return value


def jwt_exp_millis(token: str) -> int:
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Unable to decode Codex access token expiry.") from exc
    expires = payload.get("exp")
    if not isinstance(expires, int | float):
        raise ProviderError("Codex access token does not include expiry metadata.")
    return int(float(expires) * 1000)


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
