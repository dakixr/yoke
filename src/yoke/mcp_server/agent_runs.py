"""Typed, read-only access to the shared managed-process agent registry."""

from __future__ import annotations

from typing import ClassVar, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from yoke.agent.tools.base import LocalTool


class AgentUsage(BaseModel):
    """Cumulative usage reported by the SDK, without inferred costs."""

    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    totalTokens: int | None = None
    inputTokens: int | None = None
    outputTokens: int | None = None
    cachedInputTokens: int | None = None
    reasoningOutputTokens: int | None = None
    toolUses: int | None = None
    durationMs: float | None = None


class AgentRunSnapshot(BaseModel):
    """Public registry fields only, never process commands, prompts or output."""

    model_config = ConfigDict(extra="ignore", strict=True)
    schemaVersion: Literal[1]
    agentId: str
    runId: str
    sessionID: str | None
    parentRunId: str | None
    parentAgentId: str | None
    runtimeSessionID: int | None
    name: str
    provider: str
    model: str | None
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    observation: Literal["live", "stale", "lost"]
    startedAt: str
    finishedAt: str | None
    lastSeenAt: str
    updatedAt: str
    version: int
    lastToolName: str | None
    errorType: str | None
    attempt: int | None = None
    taskId: str | None = None
    originToolCallId: str | None = None
    originTurnId: str | None = None
    effort: str | None = None
    typedUsage: AgentUsage | None = None


class AgentRunsOutput(BaseModel):
    """One filtered snapshot, with counts before the result limit."""

    ok: Literal[True]
    data: list[AgentRunSnapshot]
    total: int = Field(
        ge=0, description="Matching retained runs before applying limit."
    )
    truncated: bool
    limit: int
    session_id: int | None
    active_only: bool


class _Registry(Protocol):
    def snapshots(self) -> list[dict[str, object]]: ...


class MCPAgentRunsTool(LocalTool):
    """List observed SDK runs without consuming any process output."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: ClassVar[str] = "agent_runs"
    description: ClassVar[str] = (
        "Read retained SDK agent run snapshots, newest start first. This runtime "
        "is shared by MCP clients; results are not scoped to a ChatGPT conversation. "
        "session_id filters the hosting managed process handle, not a conversation "
        "or OS PID. active_only includes only running runs with live observation. "
        "Stale or lost observation does not prove execution stopped. Reads do not "
        "start commands, change agents, or consume process output or cursors."
    )
    session_id: int | None = Field(
        default=None,
        ge=1,
        description="Filter runtimeSessionID, the hosting managed process handle.",
    )
    active_only: bool = Field(
        default=False,
        description="Return only status=running with observation=live.",
    )
    limit: int = Field(default=50, ge=1, le=200)

    def execute(self) -> dict[str, object]:
        """Read once, filter, and project only the public snapshot fields."""
        manager = self._context.get("command_process_manager")
        registry = getattr(manager, "agent_runs", None)
        if registry is None:
            raise RuntimeError("Agent run tracking is unavailable.")
        runs = [
            run
            for run in cast(_Registry, registry).snapshots()
            if (
                self.session_id is None
                or run.get("runtimeSessionID") == self.session_id
            )
            and (
                not self.active_only
                or (run.get("status") == "running" and run.get("observation") == "live")
            )
        ]
        runs.sort(key=_order, reverse=True)
        return AgentRunsOutput(
            ok=True,
            data=[AgentRunSnapshot.model_validate(run) for run in runs[: self.limit]],
            total=len(runs),
            truncated=len(runs) > self.limit,
            limit=self.limit,
            session_id=self.session_id,
            active_only=self.active_only,
        ).model_dump(exclude_unset=True)


def _order(run: dict[str, object]) -> tuple[str, str]:
    started, run_id = run.get("startedAt"), run.get("runId")
    return (
        started if isinstance(started, str) else "",
        run_id if isinstance(run_id, str) else "",
    )
