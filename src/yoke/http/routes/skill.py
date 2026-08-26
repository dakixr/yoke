"""Skill catalog and activation routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.models.prompt import PromptInput
from yoke.http.models.skill import SessionSkillResponse
from yoke.http.models.skill import SkillActivateData
from yoke.http.models.skill import SkillActivateRequest
from yoke.http.models.skill import SkillActivateResponse
from yoke.http.models.skill import SkillInfo
from yoke.http.models.skill import SkillListResponse
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.skill_service import SkillService


router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/skill", response_model=SkillListResponse, operation_id="listSkills")
def list_skills(
    request: Request,
    directory: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> SkillListResponse:
    service: SkillService = request.app.state.skill_service
    return service.list_skills(directory=directory, search=search)


@router.get(
    "/session/{session_id}/skill",
    response_model=SessionSkillResponse,
    operation_id="listSessionSkills",
)
def list_session_skills(request: Request, session_id: str) -> SessionSkillResponse:
    service: SkillService = request.app.state.skill_service
    return service.session_skills(session_id)


@router.post(
    "/session/{session_id}/skill/{skill_name}/activate",
    response_model=SkillActivateResponse,
    operation_id="activateSessionSkill",
)
async def activate_session_skill(
    request: Request,
    session_id: str,
    skill_name: str,
    body: SkillActivateRequest,
) -> SkillActivateResponse:
    service: SkillService = request.app.state.skill_service
    service.session_skills(session_id)
    runtimes: SessionRuntimeRegistry = request.app.state.runtime_registry
    activated = await runtimes.activate_skill(session_id, skill_name)
    input_id: str | None = None
    if body.prompt is not None and body.prompt.strip():
        pending: PendingInputService = request.app.state.pending_input_service
        receipt = pending.admit(
            session_id,
            PromptAdmissionRequest(
                prompt=PromptInput(text=body.prompt),
                delivery="steer",
                resume=True,
            ),
        )
        input_id = receipt.id
        await runtimes.wake(session_id)
    return SkillActivateResponse(
        data=SkillActivateData(
            activated=SkillInfo(
                name=activated.name,
                description=activated.description,
                source_path=activated.source_path,
                active=True,
            ),
            prompt_input_id=input_id,
        )
    )
