from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from threading import Event
import time
from types import SimpleNamespace

import pytest

from yoke.agent.models import Message
from yoke.http.services.session_runtime.workspace import pause_workspace_input
from yoke.session.admissions import (
    AdmissionAttachment,
    AdmissionRecord,
    AdmissionSnapshot,
)
from yoke.session.queue import PromptQueueTransaction
from yoke.session.workspace import WorkspaceBusy, relocate_session_workspace

from .test_workspace_recovery import (
    TestProvider,
    eventually_relocate,
    req,
    relocate,
    run_prompt,
)
from .test_workspace_recovery import harness as harness


def quiescent(h):
    runtime = h.app.state.runtime_registry.get_if_loaded("saved")
    assert runtime is not None
    deadline = time.monotonic() + 3
    while runtime.resources.has_live_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not runtime.resources.has_live_work()
    return runtime


def test_deletion_during_provider_call_preserves_the_completed_answer(
    harness, monkeypatch
):
    h = harness

    def complete(provider, messages, tools):
        h.calls.append([message.plain_text_content for message in messages])
        h.root.rmdir()
        return Message.assistant("answer completed before detecting deletion")

    monkeypatch.setattr(TestProvider, "complete", complete)
    status = run_prompt(h, "keep this question", input_id="completed-before-deletion")
    assert status["state"] == "error"
    assert "workspace" in status["lastError"].lower()
    assert "finalization failed" not in status["lastError"].lower()
    record = h.store.load("saved")
    assert [message.plain_text_content for message in record.messages[-2:]] == [
        "keep this question",
        "answer completed before detecting deletion",
    ]
    admission = h.app.state.pending_input_service.admissions.load("saved").records[
        "completed-before-deletion"
    ]
    assert admission.settled and admission.outcome == "completed"
    assert not h.root.exists()


def test_deletion_after_agent_preparation_pauses_input_before_provider_call(
    harness, monkeypatch
):
    h = harness
    runtime = h.app.state.runtime_registry.get_or_start("saved")
    original = runtime._prepare_turn_agent

    def prepare(*args, **kwargs):
        agent = original(*args, **kwargs)
        h.root.rmdir()
        return agent

    monkeypatch.setattr(runtime, "_prepare_turn_agent", prepare)
    admitted = req(
        h,
        "POST",
        "/session/saved/prompt",
        json={
            "id": "delete-after-prepare",
            "prompt": {"text": "retain me"},
            "resume": True,
        },
    )
    assert admitted.status_code == 200
    waited = req(h, "POST", "/session/saved/wait", params={"timeoutMs": 3000}).json()[
        "data"
    ]
    assert waited["state"] == "error"
    assert not h.calls
    queue = req(h, "GET", "/session/saved/queue").json()["data"]
    assert [(item["id"], item["paused"]) for item in queue["items"]] == [
        ("delete-after-prepare", True)
    ]
    assert h.store.load("saved").conversation_entries == h.record.conversation_entries


def test_relocation_after_cross_provider_switch_closes_each_provider_once(
    harness, monkeypatch
):
    h = harness
    assert run_prompt(h)["state"] == "idle"
    runtime = quiescent(h)
    primary = runtime.resources.primary_locked()
    original = primary.provider

    class ReplacementProvider(TestProvider):
        provider_name = "replacement"

    replacement = ReplacementProvider(h.calls, h.closed)
    replacement.config.model = "replacement-model"
    monkeypatch.setattr(
        "yoke.agent.provider_selection.build_provider",
        lambda *_args, **_kwargs: replacement,
    )
    switched = req(
        h,
        "POST",
        "/session/saved/selection",
        json={"provider": "replacement", "model": "replacement-model"},
    )
    assert switched.status_code == 200, switched.text
    assert primary.provider is replacement
    assert id(replacement) in runtime.resources._providers
    moved = eventually_relocate(h)
    assert moved.status_code == 200, moved.text
    assert h.closed.count(id(original)) == 1
    assert h.closed.count(id(replacement)) == 1
    assert primary.closed
    assert h.store.load("saved").root == str(h.target)


def test_failed_relocation_keeps_the_existing_agent_and_tool_policy(harness):
    h = harness
    assert run_prompt(h)["state"] == "idle"
    runtime = quiescent(h)
    primary = runtime.resources.primary_locked()
    changed = req(
        h, "PATCH", "/session/saved/tool", json={"disabled": ["exec_command"]}
    )
    assert changed.status_code == 200, changed.text
    enabled = runtime.session_enabled_tool_names()
    assert enabled is not None and "exec_command" not in enabled
    queued = req(
        h,
        "POST",
        "/session/saved/prompt",
        json={"prompt": {"text": "pending"}, "resume": False},
    )
    assert queued.status_code == 200
    refused = relocate(h)
    assert refused.status_code == 409
    assert runtime.resources.primary_locked() is primary
    assert not primary.closed
    assert runtime.session_enabled_tool_names() == enabled
    assert h.store.load("saved").root == str(h.root)


def test_live_mcp_inspection_excludes_relocation_until_manager_closes(
    harness, monkeypatch
):
    h = harness
    from yoke.mcp import McpManager

    started, release = Event(), Event()

    class Manager:
        def inspect(self, **_kwargs):
            started.set()
            assert release.wait(3)
            return {"servers": []}

        def close(self):
            return None

    monkeypatch.setattr(
        "yoke.mcp.config.load_mcp_config",
        lambda **_kwargs: SimpleNamespace(enabled_servers=[object()], servers=[]),
    )
    monkeypatch.setattr(McpManager, "from_paths", lambda **_kwargs: Manager())

    with ThreadPoolExecutor(max_workers=1) as requests:
        pending = requests.submit(
            req,
            h,
            "GET",
            "/session/saved/mcp?includeTools=true",
        )
        assert started.wait(2)
        refused = relocate(h)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "session_workspace_busy"
        release.set()
        inspected = pending.result(timeout=3)
    assert inspected.status_code == 200
    moved = eventually_relocate(h)
    assert moved.status_code == 200, moved.text


def test_external_relocation_invalidates_cached_skills_before_the_next_prompt(harness):
    h = harness
    for directory, content in [(h.root, "OLD_CONTENT"), (h.target, "NEW_CONTENT")]:
        path = directory / ".yoke" / "skills" / "custom" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: custom\ndescription: Workspace test.\n---\n" + content
        )
    assert run_prompt(h)["state"] == "idle"
    runtime = quiescent(h)
    original = runtime.resources.primary_locked()
    code = "from pathlib import Path; from yoke.session import SessionStore; from yoke.session.workspace import relocate_session_workspace; import sys; relocate_session_workspace(SessionStore(Path(sys.argv[1])), 'saved', sys.argv[2])"
    subprocess.run(
        [sys.executable, "-c", code, str(h.store.directory), str(h.target)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    activated = req(h, "POST", "/session/saved/skill/custom/activate", json={})
    assert activated.status_code == 200, activated.text
    assert str(h.target) in activated.json()["data"]["activated"]["sourcePath"]
    assert "NEW_CONTENT" in h.store.load("saved").active_skills[-1].content
    assert runtime.resources.primary_locked() is not original
    assert run_prompt(h, "new workspace")["state"] == "idle"


def test_background_command_keeps_a_lease_after_its_turn_is_idle(harness):
    h = harness
    assert run_prompt(h)["state"] == "idle"
    runtime = quiescent(h)
    manager = runtime.process_manager()
    assert manager is not None
    try:
        result = manager.exec_argv(
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            display_command="workspace lease test",
            cwd=h.root,
            env=dict(os.environ),
            yield_time_ms=1,
            timeout_seconds=None,
            cancel_requested=None,
        )
        assert result.session_id is not None
        assert not runtime.resources._turn_agents
        with pytest.raises(WorkspaceBusy):
            relocate_session_workspace(h.store, "saved", h.target)
        assert relocate(h).status_code == 409
    finally:
        manager.terminate_all()
    deadline = time.monotonic() + 3
    while runtime.resources.has_live_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert eventually_relocate(h).status_code == 200


@pytest.mark.parametrize("failure", ["intent", "queue", "finish"])
def test_workspace_requeue_recovers_after_each_interrupted_write(
    harness, monkeypatch, failure
):
    h = harness
    runtime = h.app.state.runtime_registry.get_or_start("saved")
    pending = h.app.state.pending_input_service
    admission = AdmissionRecord(
        id="blocked",
        session_id="saved",
        prompt="keep this exact input",
        attachments=[
            AdmissionAttachment(
                uri="upload://retained", name="image.png", mime="image/png"
            )
        ],
        delivery="steer",
        fingerprint="identity",
        time_created="now",
        admitted_seq=1,
        state="promoted",
    )
    pending.admissions.save(
        "saved", AdmissionSnapshot(records={admission.id: admission})
    )
    h.root.rmdir()
    original_save = pending.admissions.save
    original_commit = PromptQueueTransaction.commit
    calls = 0

    def save(session_id, snapshot):
        nonlocal calls
        calls += 1
        if (failure == "intent" and calls == 1) or (failure == "finish" and calls == 2):
            raise OSError("disk full")
        original_save(session_id, snapshot)

    def commit(transaction):
        if failure == "queue":
            raise OSError("disk full")
        original_commit(transaction)

    with monkeypatch.context() as patch:
        patch.setattr(pending.admissions, "save", save)
        patch.setattr(PromptQueueTransaction, "commit", commit)
        with pytest.raises(OSError, match="disk full"):
            pause_workspace_input(runtime, admission)
    # Original promoted identity remains recoverable until the queue is durable.
    assert pending.admissions.load("saved").records["blocked"].state == "promoted"
    queue = req(h, "GET", "/session/saved/queue")
    assert queue.status_code == 200, queue.text
    item = queue.json()["data"]["items"][0]
    assert item["id"] == "blocked" and item["paused"] is True
    assert item["prompt"]["text"] == admission.prompt
    assert item["prompt"]["attachments"] == [
        {"type": "file", **attachment.model_dump()}
        for attachment in admission.attachments
    ]
    repaired = pending.admissions.load("saved").records["blocked"]
    assert repaired.state == "admitted" and not repaired.workspace_blocked
    assert repaired.fingerprint == admission.fingerprint
    assert not h.calls and not h.root.exists()
    repeated = req(h, "GET", "/session/saved/queue").json()
    assert repeated == queue.json()
    assert relocate(h).status_code == 409


def test_a_promoted_input_waiting_for_capacity_is_not_mistaken_for_an_orphan(harness):
    h = harness
    registry = h.app.state.runtime_registry
    registry.active_slots = asyncio.Semaphore(0)
    try:
        admitted = req(
            h,
            "POST",
            "/session/saved/prompt",
            json={"id": "reserved", "prompt": {"text": "waiting"}, "resume": True},
        )
        assert admitted.status_code == 200
        h.root.rmdir()
        assert req(h, "GET", "/session/saved/queue").json()["data"]["items"] == []
        assert (
            registry.pending_inputs.admissions.load("saved").records["reserved"].state
            == "promoted"
        )
        with pytest.raises(WorkspaceBusy):
            relocate_session_workspace(h.store, "saved", h.target)
    finally:
        h.client.portal.call(registry.active_slots.release)
    waited = req(h, "POST", "/session/saved/wait", params={"timeoutMs": 3000}).json()[
        "data"
    ]
    assert waited["state"] == "error"
    assert (
        req(h, "GET", "/session/saved/queue").json()["data"]["items"][0]["id"]
        == "reserved"
    )
    assert not h.calls


def test_cancelled_relocation_request_does_not_cancel_the_owned_transaction(
    harness, monkeypatch
):
    h = harness
    from yoke.http.services.session_runtime import workspace as module

    original = module._relocate_sync
    started, release = Event(), Event()

    def blocked(*args):
        started.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(module, "_relocate_sync", blocked)
    registry = h.app.state.runtime_registry
    caller = None

    async def begin():
        return asyncio.create_task(registry.relocate("saved", str(h.target)))

    async def cancel(task):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        caller = h.client.portal.call(begin)
        assert started.wait(2)
        assert req(h, "GET", "/health").status_code == 200
        h.client.portal.call(cancel, caller)
        assert registry._workspace_tasks
        assert h.store.load("saved").root == str(h.root)
    finally:
        release.set()

    async def finish():
        await asyncio.gather(*registry._workspace_tasks)

    h.client.portal.call(finish)
    assert h.store.load("saved").root == str(h.target)
    assert run_prompt(h)["state"] == "idle"
