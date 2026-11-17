"""Protocol 2 input routes publish authority while retaining legacy events."""

from fastapi.testclient import TestClient

from tests.yoke.http.fake_session_agent import FakeSessionAgent
from tests.yoke.http.test_input_lifecycle_stateful import service
from tests.yoke.http.test_runtime_ownership import _execution
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.models.prompt import PromptAdmissionRequest, QueuePatchRequest


def test_legacy_events_keep_their_original_order(tmp_path):
    pending = service(tmp_path / "sessions")
    pending.admit(
        "inputs",
        PromptAdmissionRequest.model_validate(
            {
                "id": "one",
                "prompt": {"text": "first"},
                "resume": False,
            }
        ),
    )
    pending.patch_queue(
        "inputs",
        QueuePatchRequest.model_validate(
            {
                "expectedRevision": 1,
                "operations": [
                    {"op": "update", "id": "one", "prompt": {"text": "edited"}}
                ],
                "resume": False,
            }
        ),
    )
    promoted = pending.pop_next("inputs", allow_queue=True)
    assert promoted is not None
    assert promoted.id == "one"
    pending.start_input("inputs", "one", 7)
    pending.settle("inputs", "one", outcome="completed")
    pending.admit(
        "inputs",
        PromptAdmissionRequest.model_validate(
            {
                "id": "two",
                "prompt": {"text": "second"},
                "resume": False,
            }
        ),
    )
    pending.patch_queue(
        "inputs",
        QueuePatchRequest.model_validate(
            {
                "expectedRevision": pending.queue("inputs").revision,
                "operations": [{"op": "remove", "id": "two"}],
                "resume": False,
            }
        ),
    )
    events, _ = pending.events.journal.history("inputs", limit=100)
    assert [
        event.type for event in events if event.type != "session.input.changed"
    ] == [
        "session.prompt.admitted",
        "session.queue.updated",
        "session.prompt.edited",
        "session.queue.updated",
        "session.prompt.promoted",
        "session.queue.updated",
        "session.prompt.settled",
        "session.prompt.admitted",
        "session.queue.updated",
        "session.prompt.removed",
        "session.queue.updated",
    ]
    changes = [event.data for event in events if event.type == "session.input.changed"]
    assert [event["phase"] for event in changes] == [
        "queued",
        "queued",
        "promoted",
        "running",
        "settled",
        "queued",
        "removed",
    ]
    assert [event["inputRevision"] for event in changes] == list(range(1, 8))
    assert changes[3]["turnID"] == 7
    assert changes[4]["outcome"] == "completed"


def app_for(tmp_path):
    app = create_app(
        HttpAppSettings(
            auth_token=None,
            session_directory=tmp_path / "sessions",
            agent_factory=FakeSessionAgent,
        )
    )
    app.state.runtime_registry.store.save("owned", [], root=tmp_path, title="Named")
    return app


def test_receipt_after_wake_snapshots_capability_and_explicit_resume(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/capabilities").json()["data"]["features"][
                "inputLifecycle"
            ]
            is True
        )
        response = client.post(
            "/api/v1/session/owned/prompt",
            json={
                "id": "first",
                "prompt": {"text": "hello"},
                "resume": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["state"] == "promoted"
        assert response.json()["data"]["phase"] in {"promoted", "running", "settled"}
        client.post("/api/v1/session/owned/wait", params={"timeoutMs": 2000})
        assert (
            client.get("/api/v1/session/owned/input/first").json()["data"]["phase"]
            == "settled"
        )
        assert client.get("/api/v1/session/owned/input").json()["data"]["inputs"] == []
        assert (
            len(
                client.get("/api/v1/session/owned/input?live=false").json()["data"][
                    "inputs"
                ]
            )
            == 1
        )
        assert (
            client.post("/api/v1/session/owned/input/first/cancel").status_code == 409
        )
        assert client.get("/api/v1/session/owned/input/missing").status_code == 404
        assert (
            client.post("/api/v1/session/owned/input/missing/cancel").status_code == 404
        )
        assert client.post("/api/v1/session/missing/queue/resume").status_code == 404
        client.post(
            "/api/v1/session/owned/prompt",
            json={
                "id": "held",
                "prompt": {"text": "later"},
                "resume": False,
            },
        )
        app.state.pending_input_service.set_hold("owned", "interrupt")
        held = client.get("/api/v1/session/owned/input").json()["data"]
        assert held["held"] == {"reason": "interrupt"}
        assert held["inputs"][0]["phase"] == "queued"
        assert client.post("/api/v1/session/owned/queue/resume").status_code == 200
        client.post("/api/v1/session/owned/wait", params={"timeoutMs": 2000})
        assert (
            client.get("/api/v1/session/owned/input/held").json()["data"]["phase"]
            == "settled"
        )
        queue = client.get("/api/v1/session/owned/queue").json()["data"]
        assert queue["held"] is None and queue["inputRevision"] > held["inputRevision"]


def test_cancel_queued_input_never_interrupts_another_clients_turn(tmp_path):
    app = app_for(tmp_path)
    pending = app.state.pending_input_service
    for input_id in ("input", "other"):
        pending.admit(
            "owned",
            PromptAdmissionRequest.model_validate(
                {
                    "id": input_id,
                    "prompt": {"text": input_id},
                    "resume": False,
                }
            ),
        )
    assert pending.pop_next("owned", allow_queue=True).id == "input"
    pending.start_input("owned", "input", 1)
    runtime = app.state.runtime_registry.get_or_start("owned")
    execution = _execution()
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(setattr, runtime.activity, "active", execution)
        response = client.post("/api/v1/session/owned/input/other/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["phase"] == "removed"
        assert runtime.activity.active is execution
        assert not execution.stop_event.is_set()
        assert (
            client.post("/api/v1/session/owned/input/other/cancel").status_code == 409
        )
        response = client.post("/api/v1/session/owned/input/input/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["phase"] == "settled"
        assert response.json()["data"]["outcome"] == "stopped"
        assert execution.stop_event.is_set()
        assert runtime.activity.active is None


def test_idle_no_resume_requests_hold_the_queue_visibly(tmp_path):
    from yoke.session.queue import has_queued_prompts

    app = app_for(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/session/owned/prompt",
            json={
                "id": "manual",
                "prompt": {"text": "later"},
                "resume": False,
            },
        )
        assert response.status_code == 200
        queue = client.get("/api/v1/session/owned/queue").json()["data"]
        assert queue["held"] == {"reason": "manual"}
        assert not has_queued_prompts(app.state.pending_input_service.store.directory)
        response = client.patch(
            "/api/v1/session/owned/queue",
            json={
                "expectedRevision": queue["revision"],
                "resume": False,
                "operations": [
                    {"op": "update", "id": "manual", "prompt": {"text": "edited"}}
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["held"] == {"reason": "manual"}
        runtime = app.state.runtime_registry.get_if_loaded("owned")
        assert runtime is not None
        assert runtime.activity.active is None
