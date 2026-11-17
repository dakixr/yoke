"""Transport models for per-user daemon status and lifecycle control."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel


class LeaseInfo(ApiModel):
    id: str
    client: str
    pid: int | None = None
    opened_at: float


class DaemonWorkInfo(ApiModel):
    running_sessions: list[str] = Field(default_factory=list)
    runnable_queues: list[str] = Field(default_factory=list)
    held_queues: list[str] = Field(default_factory=list)
    mutations: int = 0
    admission_changed: bool = False


class DaemonStatusData(ApiModel):
    pid: int
    build_id: str
    protocol_version: str
    database_path: str
    socket: str | None = None
    port: int | None = None
    idle_exit: bool
    busy: bool
    restart_pending: bool
    leases: list[LeaseInfo]
    phase: Literal["serving", "draining", "exiting"] = "serving"
    pending_exit: Literal["restart", "stop"] | None = None
    exit_reason: Literal["idle", "restart", "stop", "signal"] | None = None
    work: DaemonWorkInfo = Field(default_factory=DaemonWorkInfo)


class DaemonStatusResponse(ApiModel):
    data: DaemonStatusData


class DaemonRestartRequest(ApiModel):
    when_idle: bool = Field(
        default=False,
        description="When busy, restart once no turn is running instead of refusing.",
    )


class DaemonStopRequest(ApiModel):
    force: bool = Field(
        default=False,
        description="Stop even while turns are running; they are interrupted.",
    )


class DaemonControlData(ApiModel):
    action: Literal["restart", "stop"]
    scheduled: Literal["now", "when_idle"]


class DaemonControlResponse(ApiModel):
    data: DaemonControlData


class DaemonIdentityData(ApiModel):
    proof: str


class DaemonIdentityResponse(ApiModel):
    data: DaemonIdentityData
