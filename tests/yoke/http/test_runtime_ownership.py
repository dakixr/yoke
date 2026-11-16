"""Loop confinement, blocking storage, and explicit session context regressions."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic


from tests.yoke.http.test_attachment_continuation import RecordingProvider
from yoke.agent.loop import RuntimeAgent
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.runtime.turns import TurnExecution
from yoke.session.admissions import AdmissionRecord


def _runtime(tmp_path: Path):  # noqa: ANN202
    app = create_app(
        HttpAppSettings(
            auth_token=None,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(RecordingProvider(), []),
        )
    )
    registry = app.state.runtime_registry
    registry.store.save("owned", [], root=tmp_path, title="Named")
    registry.pending_inputs.admit(
        "owned",
        PromptAdmissionRequest.model_validate(
            {"id": "input", "prompt": {"text": "hello"}, "resume": False}
        ),
    )
    assert registry.pending_inputs.pop_next("owned", allow_queue=True) is not None
    return registry, registry.get_or_start("owned")


def _execution() -> TurnExecution:
    return TurnExecution(
        turn_id=1,
        admission=AdmissionRecord(
            id="input",
            session_id="owned",
            prompt="hello",
            delivery="queue",
            fingerprint="fixture",
            time_created="2026-01-01T00:00:00+00:00",
            admitted_seq=1,
        ),
        started_at="2026-01-01T00:00:00+00:00",
        started_monotonic=monotonic(),
        stop_event=Event(),
        retired_event=Event(),
        cold_start=False,
        automatic_title=False,
    )
