"""Process-local human-input requests shared by workers and HTTP clients."""

from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Generic, Literal, TypeVar, cast

from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.human_input import (
    PermissionRequestInfo,
    PermissionResolution,
    QuestionRequestInfo,
    QuestionResolution,
    ResolvedBy,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.human_input_state import (
    Consumed,
    Expired,
    Pending,
    RequestEvent,
    RequestState,
    next_request_state,
    phase_of,
    resolution_of,
)
from yoke.session import SessionStore

LOGGER = logging.getLogger(__name__)
MAX_CONSUMED_REQUESTS = 64

Info = TypeVar("Info", PermissionRequestInfo, QuestionRequestInfo)
Resolution = PermissionResolution | QuestionResolution


@dataclass(slots=True)
class _Request(Generic[Info]):
    info: Info
    event: Event = field(default_factory=Event)
    state: RequestState = field(default_factory=Pending)


class HumanInputService:
    """Coordinate human replies without coupling workers to terminal input."""

    def __init__(self, store: SessionStore, events: EventService) -> None:
        self.store = store
        self.events = events
        self._lock = Lock()
        self._permissions: dict[str, _Request[PermissionRequestInfo]] = {}
        self._questions: dict[str, _Request[QuestionRequestInfo]] = {}
        self._consumed_permissions: deque[str] = deque()
        self._consumed_questions: deque[str] = deque()

    def create_permission(
        self,
        session_id: str,
        *,
        permission: str,
        message: str,
        tool_call: dict[str, object] | None = None,
    ) -> PermissionRequestInfo:
        record = self._require_session(session_id)
        info = PermissionRequestInfo(
            id=f"req_{secrets.token_hex(12)}",
            session_id=session_id,
            permission=permission,
            message=message,
            created_at=datetime.now(UTC).isoformat(),
            tool_call=tool_call,
        )
        with self._lock:
            self._permissions[info.id] = _Request(
                info=info,
                state=next_request_state(None, RequestEvent.CREATE),
            )
        self.events.live(
            "session.permission.requested",
            info.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=record.root,
        )
        return info.model_copy(deep=True)

    def permissions(self, session_id: str) -> list[PermissionRequestInfo]:
        self._require_session(session_id)
        with self._lock:
            return [
                request.info.model_copy(deep=True)
                for request in self._permissions.values()
                if request.info.session_id == session_id
                and isinstance(request.state, Pending)
            ]

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        reply: Literal["allow", "deny"],
        message: str | None = None,
    ) -> PermissionResolution:
        record = self._require_session(session_id)
        if reply not in {"allow", "deny"}:
            raise ApiError(400, "invalid_permission_reply", "Invalid permission reply.")
        resolution = PermissionResolution(
            request_id=request_id,
            reply=reply,
            message=message,
            by=ResolvedBy.USER,
        )
        with self._lock:
            request = self._permission_request(session_id, request_id)
            if not isinstance(request.state, Pending):
                raise self._already_resolved(request.state)
            request.state = next_request_state(
                request.state,
                RequestEvent.ANSWER,
                value=resolution,
                by=ResolvedBy.USER,
            )
            request.event.set()
        self._publish_permission(session_id, record.root, resolution)
        return resolution.model_copy(deep=True)

    def cancel_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        by: ResolvedBy,
    ) -> PermissionResolution | None:
        """Release a waiter when its turn stops. Repeat calls are idempotent."""
        record = self._require_session(session_id)
        denied = PermissionResolution(request_id=request_id, reply="deny", by=by)
        resolution = self._cancel(
            self._permissions,
            session_id,
            request_id,
            by=by,
            resolution=denied,
            not_found=("permission_not_found", "Permission request was not found."),
            publish=lambda value: self._publish_permission(
                session_id, record.root, cast(PermissionResolution, value)
            ),
        )
        return cast(PermissionResolution | None, resolution)

    def wait_permission(
        self,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> PermissionResolution:
        request = self._waiting_request(self._permissions, request_id, timeout_seconds)
        resolution = self._consume(
            self._permissions,
            self._consumed_permissions,
            request,
            request_id,
        )
        return cast(PermissionResolution, resolution).model_copy(deep=True)

    def create_question(
        self,
        session_id: str,
        *,
        question: str,
        options: list[str] | None = None,
        multiple: bool = False,
    ) -> QuestionRequestInfo:
        record = self._require_session(session_id)
        info = QuestionRequestInfo(
            id=f"req_{secrets.token_hex(12)}",
            session_id=session_id,
            question=question,
            options=list(options or []),
            multiple=multiple,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self._questions[info.id] = _Request(
                info=info,
                state=next_request_state(None, RequestEvent.CREATE),
            )
        self.events.live(
            "session.question.requested",
            info.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=record.root,
        )
        return info.model_copy(deep=True)

    def questions(self, session_id: str) -> list[QuestionRequestInfo]:
        self._require_session(session_id)
        with self._lock:
            return [
                request.info.model_copy(deep=True)
                for request in self._questions.values()
                if request.info.session_id == session_id
                and isinstance(request.state, Pending)
            ]

    def reply_question(
        self,
        session_id: str,
        request_id: str,
        *,
        answers: list[str],
    ) -> QuestionResolution:
        if not answers:
            raise ApiError(
                400,
                "empty_question_reply",
                "Question reply cannot be empty.",
            )
        return self._answer_question(
            session_id,
            request_id,
            answers=answers,
            rejected=False,
        )

    def reject_question(self, session_id: str, request_id: str) -> QuestionResolution:
        return self._answer_question(
            session_id,
            request_id,
            answers=[],
            rejected=True,
        )

    def cancel_question(
        self,
        session_id: str,
        request_id: str,
        *,
        by: ResolvedBy,
    ) -> QuestionResolution | None:
        """Release a waiter when its turn stops. Repeat calls are idempotent."""
        record = self._require_session(session_id)
        rejected = QuestionResolution(request_id=request_id, rejected=True, by=by)
        resolution = self._cancel(
            self._questions,
            session_id,
            request_id,
            by=by,
            resolution=rejected,
            not_found=("question_not_found", "Question request was not found."),
            publish=lambda value: self._publish_question(
                session_id, record.root, cast(QuestionResolution, value)
            ),
        )
        return cast(QuestionResolution | None, resolution)

    def wait_question(
        self,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> QuestionResolution:
        request = self._waiting_request(self._questions, request_id, timeout_seconds)
        resolution = self._consume(
            self._questions,
            self._consumed_questions,
            request,
            request_id,
        )
        return cast(QuestionResolution, resolution).model_copy(deep=True)

    def cancel_session(self, session_id: str) -> int:
        """Cancel every pending request when a client session goes away."""
        record = self._require_session(session_id)
        with self._lock:
            permissions = [
                request
                for request in self._permissions.values()
                if request.info.session_id == session_id
                and isinstance(request.state, Pending)
            ]
            questions = [
                request
                for request in self._questions.values()
                if request.info.session_id == session_id
                and isinstance(request.state, Pending)
            ]
            for request in permissions:
                request.state = next_request_state(
                    request.state,
                    RequestEvent.CANCEL,
                    value=PermissionResolution(
                        request_id=request.info.id,
                        reply="deny",
                        by=ResolvedBy.SESSION_CLOSED,
                    ),
                    by=ResolvedBy.SESSION_CLOSED,
                )
                request.event.set()
            for request in questions:
                request.state = next_request_state(
                    request.state,
                    RequestEvent.CANCEL,
                    value=QuestionResolution(
                        request_id=request.info.id,
                        rejected=True,
                        by=ResolvedBy.SESSION_CLOSED,
                    ),
                    by=ResolvedBy.SESSION_CLOSED,
                )
                request.event.set()
            permission_resolutions = [
                cast(PermissionResolution, resolution_of(request.state))
                for request in permissions
            ]
            question_resolutions = [
                cast(QuestionResolution, resolution_of(request.state))
                for request in questions
            ]
        for resolution in permission_resolutions:
            self._publish_permission(session_id, record.root, resolution)
        for resolution in question_resolutions:
            self._publish_question(session_id, record.root, resolution)
        return len(permissions) + len(questions)

    def _answer_question(
        self,
        session_id: str,
        request_id: str,
        *,
        answers: list[str],
        rejected: bool,
    ) -> QuestionResolution:
        record = self._require_session(session_id)
        resolution = QuestionResolution(
            request_id=request_id,
            answers=list(answers),
            rejected=rejected,
            by=ResolvedBy.USER,
        )
        with self._lock:
            request = self._question_request(session_id, request_id)
            if not isinstance(request.state, Pending):
                raise self._already_resolved(request.state)
            if not rejected:
                self._validate_question_answers(request.info, answers)
            request.state = next_request_state(
                request.state,
                RequestEvent.ANSWER,
                value=resolution,
                by=ResolvedBy.USER,
            )
            request.event.set()
        self._publish_question(session_id, record.root, resolution)
        return resolution.model_copy(deep=True)

    def _cancel(
        self,
        table: dict[str, _Request[Info]],
        session_id: str,
        request_id: str,
        *,
        by: ResolvedBy,
        resolution: PermissionResolution | QuestionResolution,
        not_found: tuple[str, str],
        publish: Callable[[Resolution], None],
    ) -> Resolution | None:
        with self._lock:
            request = table.get(request_id)
            if request is None or request.info.session_id != session_id:
                raise ApiError(404, *not_found)
            state = request.state
            if not isinstance(state, Pending):
                stored = resolution_of(state)
                return None if stored is None else cast(Resolution, stored)
            request.state = next_request_state(
                state,
                RequestEvent.CANCEL,
                value=resolution,
                by=by,
            )
            request.event.set()
        publish(resolution)
        return resolution

    def _waiting_request(
        self,
        table: dict[str, _Request[Info]],
        request_id: str,
        timeout_seconds: float | None,
    ) -> _Request[Info]:
        with self._lock:
            request = table.get(request_id)
            if request is None:
                raise KeyError(request_id)
            pending = isinstance(request.state, Pending)
        if pending and not request.event.wait(timeout_seconds):
            raise TimeoutError(request_id)
        with self._lock:
            state = request.state
        if isinstance(state, (Expired, Consumed)):
            raise KeyError(request_id)
        return request

    def _consume(
        self,
        table: dict[str, _Request[Info]],
        order: deque[str],
        request: _Request[Info],
        request_id: str,
    ) -> object:
        with self._lock:
            state = request.state
            if isinstance(state, (Expired, Consumed)):
                raise KeyError(request_id)
            resolution = resolution_of(state)
            if resolution is None:
                raise RuntimeError("Request signaled without a resolution.")
            request.state = next_request_state(state, RequestEvent.CONSUME)
            order.append(request_id)
            while len(order) > MAX_CONSUMED_REQUESTS:
                table.pop(order.popleft(), None)
        return resolution

    def _permission_request(
        self, session_id: str, request_id: str
    ) -> _Request[PermissionRequestInfo]:
        request = self._permissions.get(request_id)
        if request is None or request.info.session_id != session_id:
            raise ApiError(
                404,
                "permission_not_found",
                "Permission request was not found.",
            )
        return request

    def _question_request(
        self, session_id: str, request_id: str
    ) -> _Request[QuestionRequestInfo]:
        request = self._questions.get(request_id)
        if request is None or request.info.session_id != session_id:
            raise ApiError(
                404,
                "question_not_found",
                "Question request was not found.",
            )
        return request

    @staticmethod
    def _already_resolved(state: RequestState) -> ApiError:
        details: dict[str, object] = {"phase": phase_of(state).value}
        resolution = resolution_of(state)
        if isinstance(resolution, (PermissionResolution, QuestionResolution)):
            details["resolution"] = resolution.model_dump(mode="json", by_alias=True)
        return ApiError(
            409,
            "already_resolved",
            "Request was already resolved.",
            details,
        )

    def _publish_permission(
        self,
        session_id: str,
        location: str | None,
        resolution: PermissionResolution,
    ) -> None:
        LOGGER.debug(
            "human_input=permission id=%s reply=%s by=%s",
            resolution.request_id,
            resolution.reply,
            resolution.by,
        )
        self.events.live(
            "session.permission.resolved",
            resolution.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=location,
        )

    def _publish_question(
        self,
        session_id: str,
        location: str | None,
        resolution: QuestionResolution,
    ) -> None:
        LOGGER.debug(
            "human_input=question id=%s rejected=%s by=%s",
            resolution.request_id,
            resolution.rejected,
            resolution.by,
        )
        self.events.live(
            "session.question.resolved",
            resolution.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=location,
        )

    @staticmethod
    def _validate_question_answers(
        info: QuestionRequestInfo, answers: list[str]
    ) -> None:
        if not info.multiple and len(answers) != 1:
            raise ApiError(
                400,
                "invalid_question_reply",
                "This question accepts exactly one answer.",
            )
        if info.options:
            invalid = [answer for answer in answers if answer not in info.options]
            if invalid:
                raise ApiError(
                    400,
                    "invalid_question_reply",
                    "Question reply contains an unsupported answer.",
                    {"invalidAnswers": invalid},
                )

    def _require_session(self, session_id: str):  # noqa: ANN202
        record = self.store.summary_record(session_id)
        if record is None:
            raise session_not_found()
        return record
