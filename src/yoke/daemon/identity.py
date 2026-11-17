"""Daemon identity proof without revealing the bearer capability to a listener."""

from __future__ import annotations

import hmac


def identity_proof(token: str, nonce: str, port: int) -> str:
    """Bind a fresh client nonce to the daemon's private discovery token."""
    return hmac.digest(
        token.encode(), f"yoke-daemon-identity-v1\0{nonce}:{port}".encode(), "sha256"
    ).hex()
