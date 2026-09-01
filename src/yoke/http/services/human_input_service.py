"""Process-local human-input requests shared by workers and HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import secrets
from threading import Event
from threading import Lock
from typing import Literal

from yoke.http.errors import ApiError
from yoke.http.models.human_input import PermissionRequestInfo
from yoke.http.models.human_input import PermissionResolution
from yoke.http.models.human_input import QuestionRequestInfo
from yoke.http.models.human_input import QuestionResolution
from yoke.http.services.event_broker import EventService
from yoke.session import SessionStore


@dataclass(slots=True)
class _PendingPermission:
    info: PermissionRequestInfo
    event: Event
    resolution: PermissionResolution | None = None


@dataclass(slots=True)
class _PendingQuestion:
    info: QuestionRequestInfo
    event: Event
    resolution: QuestionResolution | None = None


class HumanInputService:
    """Coordinate human replies without coupling workers to terminal input."""

    def __init__(self, store: SessionStore, events: EventService) -> None:
        self.store = store
        self.events = events
        self._lock = Lock()
        self._permissions: dict[str, _PendingPermission] = {}
        self._questions: dict[str, _PendingQuestion] = {}

    def create_permission(
        self,
        session_id: str,
        *,
        permission: str,
        message: str,
    ) -> PermissionRequestInfo:
        record = self._require_session(session_id)
        info = PermissionRequestInfo(
            id=f"req_{secrets.token_hex(12)}",
            session_id=session_id,
            permission=permission,
            message=message,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self._permissions[info.id] = _PendingPermission(info=info, event=Event())
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
                pending.info.model_copy(deep=True)
                for pending in self._permissions.values()
                if pending.info.session_id == session_id and pending.resolution is None
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
        with self._lock:
            pending = self._permissions.get(request_id)
            if pending is None or pending.info.session_id != session_id:
                raise ApiError(
                    404,
                    "permission_not_found",
                    "Permission request was not found.",
                )
            if pending.resolution is not None:
                return pending.resolution.model_copy(deep=True)
            resolution = PermissionResolution(
                request_id=request_id,
                reply=reply,
                message=message,
            )
            pending.resolution = resolution
            pending.event.set()
        self.events.live(
            "session.permission.resolved",
            resolution.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=record.root,
        )
        return resolution.model_copy(deep=True)

    def wait_permission(
        self,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> PermissionResolution:
        with self._lock:
            pending = self._permissions.get(request_id)
        if pending is None:
            raise KeyError(request_id)
        if not pending.event.wait(timeout_seconds):
            raise TimeoutError(request_id)
        resolution = pending.resolution
        if resolution is None:
            raise RuntimeError("Permission request signaled without a resolution.")
        with self._lock:
            self._permissions.pop(request_id, None)
        return resolution.model_copy(deep=True)

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
            self._questions[info.id] = _PendingQuestion(info=info, event=Event())
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
                pending.info.model_copy(deep=True)
                for pending in self._questions.values()
                if pending.info.session_id == session_id and pending.resolution is None
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
        return self._resolve_question(
            session_id,
            request_id,
            answers=answers,
            rejected=False,
        )

    def reject_question(self, session_id: str, request_id: str) -> QuestionResolution:
        return self._resolve_question(
            session_id,
            request_id,
            answers=[],
            rejected=True,
        )

    def wait_question(
        self,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> QuestionResolution:
        with self._lock:
            pending = self._questions.get(request_id)
        if pending is None:
            raise KeyError(request_id)
        if not pending.event.wait(timeout_seconds):
            raise TimeoutError(request_id)
        resolution = pending.resolution
        if resolution is None:
            raise RuntimeError("Question request signaled without a resolution.")
        with self._lock:
            self._questions.pop(request_id, None)
        return resolution.model_copy(deep=True)

    def _resolve_question(
        self,
        session_id: str,
        request_id: str,
        *,
        answers: list[str],
        rejected: bool,
    ) -> QuestionResolution:
        record = self._require_session(session_id)
        with self._lock:
            pending = self._questions.get(request_id)
            if pending is None or pending.info.session_id != session_id:
                raise ApiError(
                    404, "question_not_found", "Question request was not found."
                )
            if pending.resolution is not None:
                return pending.resolution.model_copy(deep=True)
            if not rejected:
                self._validate_question_answers(pending.info, answers)
            resolution = QuestionResolution(
                request_id=request_id,
                answers=list(answers),
                rejected=rejected,
            )
            pending.resolution = resolution
            pending.event.set()
        self.events.live(
            "session.question.resolved",
            resolution.model_dump(mode="json", by_alias=True),
            session_id=session_id,
            location=record.root,
        )
        return resolution.model_copy(deep=True)

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
            raise ApiError(404, "session_not_found", "Session was not found.")
        return record
