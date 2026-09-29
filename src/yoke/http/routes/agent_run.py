"""Authenticated session-owned SDK run snapshots."""

from fastapi import APIRouter, Depends, Query, Request

from yoke.http.auth import require_auth
from yoke.http.models.agent_run import AgentRunListResponse, AgentRunSnapshot
from yoke.http.services.agent_runs import AgentRunService

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get(
    "/agent-run",
    response_model=AgentRunListResponse,
    response_model_exclude_unset=True,
    operation_id="listAgentRuns",
)
def list_agent_runs(
    request: Request, session_id: str = Query(alias="sessionID", min_length=1)
) -> AgentRunListResponse:
    service: AgentRunService = request.app.state.agent_run_service
    return AgentRunListResponse(
        data=[
            AgentRunSnapshot.model_validate(row)
            for row in service.snapshots(session_id)
        ]
    )
