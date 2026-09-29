"""Validated public records and private launch authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

from yoke.agent_runs.lifetime import Lifetime
from yoke.agent_runs.worker import Worker


def text(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Invalid agent run label")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Invalid agent run label")
    return value


def task_label(value: str) -> str:
    """Bound optional metadata before JSON framing, without changing task IDs."""
    if 0 < len(value) <= 256 and all(char.isprintable() for char in value):
        return value
    prefix = "".join(char if char.isprintable() else " " for char in value[:230])
    digest = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return f"{prefix.strip()}~{digest}"


@dataclass(frozen=True)
class Owner:
    session_id: str | None = None
    runtime_session_id: int | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None


@dataclass
class Capability:
    owner: Owner
    run_id: str | None = None
    lifetime: Lifetime | None = None
    owns_lifetime: bool = False


@dataclass
class Record:
    snapshot: dict[str, object]
    launch_token: str
    token: str
    worker: Worker
    seen: float
    sequence: int = 0
    terminal_at: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def metadata(frame: dict[str, object]) -> dict[str, object]:
    """Copy an allowlist. Never retain prompts, arguments, outputs or credentials."""
    result: dict[str, object] = {
        key: text(
            frame.get(key), required=key in {"agentId", "runId", "provider", "name"}
        )
        for key in ("agentId", "runId", "provider", "model", "name")
    }
    for key in ("taskId",):
        if key in frame:
            result[key] = text(frame[key])
    if "attempt" in frame:
        attempt = frame["attempt"]
        if type(attempt) is not int or not 1 <= attempt <= 1_000_000:
            raise ValueError("Invalid agent run attempt")
        result["attempt"] = attempt
    return result
