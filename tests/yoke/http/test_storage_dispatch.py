"""Request storage and catalog failures keep the daemon responsive/observable."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest
from fastapi.testclient import TestClient

from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for
from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session
from yoke.http.services import catalog_service


@pytest.mark.parametrize("route", ["admit", "queue", "metadata"])
def test_request_storage_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    app = app_for(tmp_path, RecordingProvider())
    started, release = Event(), Event()
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as caller:
        _create_session(client, tmp_path, "session-a")
        assert client.portal is not None
        loop_thread = client.portal.call(get_ident)
        if route == "admit":
            owner, method = app.state.pending_input_service, "admit"
            verb, path, body = (
                "POST",
                "prompt",
                {"prompt": {"text": "queued"}, "resume": False},
            )
        elif route == "queue":
            owner, method = app.state.pending_input_service, "patch_queue"
            verb, path, body = (
                "PATCH",
                "queue",
                {"expectedRevision": 0, "operations": []},
            )
        else:
            owner, method = app.state.session_service, "patch_session"
            verb, path, body = "PATCH", "", {"title": "Renamed"}
        original = getattr(owner, method)

        def blocked(*args, **kwargs):  # noqa: ANN202
            assert get_ident() != loop_thread
            started.set()
            assert release.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, method, blocked)
        response = caller.submit(
            client.request,
            verb,
            f"/api/v1/session/session-a{'/' + path if path else ''}",
            headers=_auth(),
            json=body,
        )
        try:
            assert started.wait(1)
            assert client.get("/api/v1/health", headers=_auth()).status_code == 200
            assert not response.done()
        finally:
            release.set()
        assert response.result(timeout=3).status_code == 200


def test_catalog_failure_warns_with_provider_and_exception_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        catalog_service, "available_provider_names", lambda **_kwargs: ["fixture"]
    )
    catalog = catalog_service.CatalogService()

    def unavailable(_provider):  # noqa: ANN202
        raise OSError("private endpoint detail")

    monkeypatch.setattr(catalog, "_provider_models", unavailable)
    with caplog.at_level("WARNING"):
        response = catalog.models(directory=str(tmp_path), provider=None, search=None)
    assert response.data == []
    assert "fixture" in caplog.text and "OSError" in caplog.text
    assert "private endpoint detail" not in caplog.text


def test_manual_title_cannot_be_overwritten_by_a_final_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from yoke.agent.loop import AgentResult
    from yoke.agent.models import ConversationEntry, Message
    from yoke.http.models.session import SessionPatchRequest
    from yoke.http.runtime.turns import TurnOutcome
    from yoke.http.services.session_service import SessionService
    from tests.yoke.http.test_runtime_ownership import _execution, _runtime

    registry, runtime = _runtime(tmp_path)
    service = SessionService(
        runtime.store,
        runtime.events,
        runtime.read_cache,
        message_index=runtime.message_index,
    )
    started, release = Event(), Event()
    original = runtime.store.save

    def blocked_save(*args, **kwargs):  # noqa: ANN202
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "save", blocked_save)

    async def scenario() -> None:
        execution = _execution()
        runtime.activity.active = execution
        outcome = TurnOutcome(
            agent=None,
            result=AgentResult(
                output="done",
                messages=[],
                iterations=1,
                conversation_entries=[
                    ConversationEntry(
                        kind="assistant", message=Message.assistant("done")
                    )
                ],
            ),
        )
        finish = asyncio.create_task(runtime.finalizer.finish(execution, outcome))
        assert await asyncio.to_thread(started.wait, 1)
        patch = asyncio.create_task(
            service.patch(registry, "owned", SessionPatchRequest(title="Manual"))
        )
        try:
            await asyncio.sleep(0.02)
            assert not patch.done()
        finally:
            release.set()
        await finish
        assert (await patch).title == "Manual"
        assert runtime.store.summary_record("owned").title == "Manual"
        await registry.close()
        service.close()

    asyncio.run(scenario())
