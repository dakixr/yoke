"""Idempotent delegation with bounded unconfirmed launch ownership."""

from __future__ import annotations

import hmac
import secrets

from yoke.agent_runs.lifetime import Lifetime
from yoke.agent_runs.records import Capability, Owner
from yoke.agent_runs.worker import Worker

MAX_UNATTACHED_LAUNCHES = 256
MAX_REMOTE_LAUNCHES = 4096


def retention_token(key: bytes, parent: str, pid: int | None, request_id: str) -> str:
    """Bind a retained instance to its caller and retry identity without a cache."""
    return hmac.digest(key, f"{parent}\0{pid}\0{request_id}".encode(), "sha256").hex()


def delegate(
    capabilities: dict[str, Capability],
    key: bytes,
    parent_token: str,
    *,
    runtime_session_id: int | None,
    pid: int | None,
    process_launch: bool,
    request_id: str | None,
) -> str:
    token = (
        retention_token(key, parent_token, pid, request_id)
        if request_id is not None and not process_launch
        else secrets.token_urlsafe(32)
    )
    # A lost ACK may be recovered after the parent's public history expired.
    # This returns existing authority only, never creates it from an expired key.
    if token in capabilities:
        return token
    parent = capabilities.get(parent_token)
    if parent is None:
        raise ValueError("Invalid launch capability")
    if parent.lifetime is not None and pid is not None:
        parent.lifetime.observe(pid)
    if process_launch and pid is not None:
        launches = [
            cap.lifetime
            for cap in capabilities.values()
            if cap.owns_lifetime
            and cap.lifetime is not None
            and cap.lifetime.process_launch
        ]
        if (
            len(launches) >= MAX_REMOTE_LAUNCHES
            or sum(scope.unattached for scope in launches) >= MAX_UNATTACHED_LAUNCHES
        ):
            raise ValueError("Unconfirmed launch authority capacity exhausted")
    owner = parent.owner
    capabilities[token] = Capability(
        Owner(
            owner.session_id,
            owner.runtime_session_id or runtime_session_id,
            owner.parent_run_id,
            owner.parent_agent_id,
        ),
        lifetime=Lifetime(
            worker=Worker.open(pid) if pid is not None else None,
            parent=parent.lifetime,
            process_launch=process_launch,
        ),
        owns_lifetime=True,
    )
    return token
