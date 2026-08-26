"""Question request HTTP resources."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.human_input import QuestionListResponse
from yoke.http.models.human_input import QuestionReplyRequest
from yoke.http.models.human_input import QuestionReplyResponse
from yoke.http.services.human_input_service import HumanInputService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> HumanInputService:
    return request.app.state.human_input_service


@router.get(
    "/session/{session_id}/question",
    response_model=QuestionListResponse,
    operation_id="listSessionQuestions",
)
def list_session_questions(request: Request, session_id: str) -> QuestionListResponse:
    return QuestionListResponse(data=_service(request).questions(session_id))


@router.post(
    "/session/{session_id}/question/{request_id}/reply",
    response_model=QuestionReplyResponse,
    operation_id="replySessionQuestion",
)
def reply_session_question(
    request: Request,
    session_id: str,
    request_id: str,
    body: QuestionReplyRequest,
) -> QuestionReplyResponse:
    return QuestionReplyResponse(
        data=_service(request).reply_question(
            session_id,
            request_id,
            answers=body.answers,
        )
    )


@router.post(
    "/session/{session_id}/question/{request_id}/reject",
    response_model=QuestionReplyResponse,
    operation_id="rejectSessionQuestion",
)
def reject_session_question(
    request: Request,
    session_id: str,
    request_id: str,
) -> QuestionReplyResponse:
    return QuestionReplyResponse(
        data=_service(request).reject_question(session_id, request_id)
    )
