"""Explicit permission and question lifecycles, including double replies."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.http.app import HttpAppSettings, create_app
from yoke.http.services.human_input_state import (
    Answered,
    Cancelled,
    Consumed,
    Expired,
    IllegalRequestTransition,
    Pending,
    RequestEvent,
    RequestPhase,
    ResolvedBy,
    next_request_state,
    phase_of,
    resolution_of,
)

TOKEN = "test-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _fastapi(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


@pytest.fixture
def human_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            HttpAppSettings(
                auth_token=TOKEN,
                session_directory=tmp_path / "sessions",
            )
        )
    ) as client:
        yield client


STATES = [
    None,
    Pending(),
    Answered(value="first"),
    Cancelled(by=ResolvedBy.TURN_STOPPED, value="first"),
    Expired(),
    Consumed(previous=Answered(value="first")),
]
EVENTS = list(RequestEvent)

LEGAL: dict[tuple[RequestPhase | None, RequestEvent], RequestPhase] = {
    (None, RequestEvent.CREATE): RequestPhase.PENDING,
    (RequestPhase.PENDING, RequestEvent.ANSWER): RequestPhase.ANSWERED,
    (RequestPhase.PENDING, RequestEvent.CANCEL): RequestPhase.CANCELLED,
    (RequestPhase.PENDING, RequestEvent.EXPIRE): RequestPhase.EXPIRED,
    (RequestPhase.ANSWERED, RequestEvent.CONSUME): RequestPhase.CONSUMED,
    (RequestPhase.CANCELLED, RequestEvent.CONSUME): RequestPhase.CONSUMED,
    (RequestPhase.EXPIRED, RequestEvent.CONSUME): RequestPhase.CONSUMED,
}


def _event_arguments(event: RequestEvent) -> dict[str, Any]:
    if event is RequestEvent.ANSWER:
        return {"value": "second", "by": ResolvedBy.USER}
    if event is RequestEvent.CANCEL:
        return {"value": "second", "by": ResolvedBy.TURN_STOPPED}
    return {}


@pytest.mark.parametrize("state", STATES, ids=lambda value: str(phase_of(value)))
@pytest.mark.parametrize("event", EVENTS, ids=lambda value: value.value)
def test_request_transition_table_covers_every_pair(state, event) -> None:
    phase: RequestPhase | None = None if state is None else phase_of(state)
    key: tuple[RequestPhase | None, RequestEvent] = (phase, event)
    arguments = _event_arguments(event)
    if key not in LEGAL:
        with pytest.raises(IllegalRequestTransition):
            next_request_state(state, event, **arguments)
        return
    result = next_request_state(state, event, **arguments)
    assert phase_of(result) is LEGAL[key]


def test_illegal_transition_rejects_a_value_without_an_answer() -> None:
    with pytest.raises(IllegalRequestTransition):
        next_request_state(Pending(), RequestEvent.ANSWER)
    with pytest.raises(IllegalRequestTransition):
        next_request_state(
            Pending(), RequestEvent.CANCEL, value="x", by=ResolvedBy.USER
        )
    with pytest.raises(IllegalRequestTransition):
        next_request_state(None, RequestEvent.ANSWER, value="x", by=ResolvedBy.USER)


def test_consumed_state_keeps_the_first_resolution() -> None:
    state = next_request_state(
        Pending(), RequestEvent.ANSWER, value="allow", by=ResolvedBy.USER
    )
    consumed = next_request_state(state, RequestEvent.CONSUME)
    assert phase_of(consumed) is RequestPhase.CONSUMED
    assert resolution_of(consumed) == "allow"


def _session(client: TestClient, tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "human-input-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200
    return "human-input-session"


def test_second_permission_reply_returns_the_first_resolution_as_conflict(
    human_client: TestClient, tmp_path: Path
) -> None:
    client = human_client
    session_id = _session(client, tmp_path)
    service = _fastapi(client).state.human_input_service
    permission = service.create_permission(
        session_id, permission="shell.write", message="Allow?"
    )

    first = client.post(
        f"/api/v1/session/{session_id}/permission/{permission.id}/reply",
        headers=_auth(),
        json={"reply": "allow", "message": "first"},
    )
    second = client.post(
        f"/api/v1/session/{session_id}/permission/{permission.id}/reply",
        headers=_auth(),
        json={"reply": "deny", "message": "second"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "already_resolved"
    assert error["details"]["phase"] == "answered"
    assert error["details"]["resolution"]["reply"] == "allow"
    assert error["details"]["resolution"]["message"] == "first"
    resolution = service.wait_permission(permission.id, 0.1)
    assert resolution.reply == "allow"
    assert resolution.by == "user"


def test_second_question_reply_returns_the_first_resolution_as_conflict(
    human_client: TestClient, tmp_path: Path
) -> None:
    client = human_client
    session_id = _session(client, tmp_path)
    service = _fastapi(client).state.human_input_service
    question = service.create_question(
        session_id, question="Which branch?", options=["main", "feature"]
    )

    first = client.post(
        f"/api/v1/session/{session_id}/question/{question.id}/reply",
        headers=_auth(),
        json={"answers": ["feature"]},
    )
    second = client.post(
        f"/api/v1/session/{session_id}/question/{question.id}/reply",
        headers=_auth(),
        json={"answers": ["main"]},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_resolved"
    assert second.json()["error"]["details"]["resolution"]["answers"] == ["feature"]
    resolution = service.wait_question(question.id, 0.1)
    assert resolution.answers == ["feature"]


def test_reply_after_a_cancel_reports_the_cancel(
    human_client: TestClient, tmp_path: Path
) -> None:
    client = human_client
    session_id = _session(client, tmp_path)
    service = _fastapi(client).state.human_input_service
    permission = service.create_permission(
        session_id, permission="shell.write", message="Allow?"
    )

    first = service.cancel_permission(
        session_id, permission.id, by=ResolvedBy.TURN_STOPPED
    )
    again = service.cancel_permission(
        session_id, permission.id, by=ResolvedBy.TURN_STOPPED
    )
    assert again == first
    reply = client.post(
        f"/api/v1/session/{session_id}/permission/{permission.id}/reply",
        headers=_auth(),
        json={"reply": "allow"},
    )
    assert reply.status_code == 409
    assert reply.json()["error"]["details"]["phase"] == "cancelled"
    assert reply.json()["error"]["details"]["resolution"]["reply"] == "deny"


def test_cancelling_a_session_resolves_every_pending_request(
    human_client: TestClient, tmp_path: Path
) -> None:
    client = human_client
    session_id = _session(client, tmp_path)
    service = _fastapi(client).state.human_input_service
    permission = service.create_permission(
        session_id, permission="shell.write", message="Allow?"
    )
    question = service.create_question(session_id, question="Which branch?")

    service.cancel_session(session_id)

    assert service.permissions(session_id) == []
    assert service.questions(session_id) == []
    assert service.wait_permission(permission.id, 0.1).reply == "deny"
    assert service.wait_question(question.id, 0.1).rejected is True
