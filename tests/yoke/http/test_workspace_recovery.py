from __future__ import annotations

from threading import Event
from types import SimpleNamespace
import time

from fastapi.testclient import TestClient
import pytest

from yoke.agent.models import Message
from yoke.cli.config import runtime as config_runtime
from yoke.http.app import HttpAppSettings, create_app
from yoke.session.workspace import WorkspaceBusy, relocate_session_workspace


TOKEN = "workspace-recovery-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestProvider:
    __test__ = False
    supports_image_inputs = False

    def __init__(self, calls, closed, gate=None):
        self.calls = calls
        self.closed = closed
        self.gate = gate
        self.config = SimpleNamespace(model="workspace-qa")

    def complete(self, messages, tools):
        self.calls.append([message.plain_text_content or "" for message in messages])
        if self.gate is not None:
            self.gate[0].set()
            if not self.gate[1].wait(5):
                raise TimeoutError("provider gate not released")
        return Message.assistant("answered")

    def fork_for_turn(self):
        return TestProvider(self.calls, self.closed, self.gate)

    def close(self):
        self.closed.append(id(self))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    calls, closed, roots = [], [], []
    gate = [None]

    def provider(args, **_kwargs):
        roots.append(args.root)
        return TestProvider(calls, closed, gate[0]), None

    monkeypatch.setattr(config_runtime, "_build_startup_provider", provider)
    root, target = tmp_path / "original", tmp_path / "target"
    root.mkdir()
    target.mkdir()
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "store")
    )
    store = app.state.session_service.store
    record = store.save(
        "saved",
        [Message.user("original question"), Message.assistant("old answer")],
        root=root,
        title="Named history",
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app,
            store=store,
            record=record,
            root=root,
            target=target,
            calls=calls,
            closed=closed,
            roots=roots,
            client=client,
            gate=gate,
        )


def req(h, method, path, **kwargs):
    return h.client.request(method, "/api/v1" + path, headers=AUTH, **kwargs)


def run_prompt(h, text="continue", input_id=None):
    body = {"prompt": {"text": text}, "resume": True}
    if input_id is not None:
        body["id"] = input_id
    admitted = req(h, "POST", "/session/saved/prompt", json=body)
    assert admitted.status_code == 200, admitted.text
    waited = req(h, "POST", "/session/saved/wait", params={"timeoutMs": 3000})
    assert waited.status_code == 200, waited.text
    return waited.json()["data"]


def relocate(h, **extra):
    return req(
        h, "POST", "/session/saved/relocate", json={"directory": str(h.target), **extra}
    )


def eventually_relocate(h):
    end = time.monotonic() + 3
    while True:
        response = relocate(h)
        if response.status_code != 409 or time.monotonic() > end:
            return response
        time.sleep(0.01)


def test_deleted_workspace_keeps_history_and_metadata_operations_available(harness):
    h = harness
    h.root.rmdir()
    for path in (
        "/session",
        "/session/saved",
        "/session/saved/message",
        "/session/saved/tree",
        "/session/saved/context",
        "/session/saved/queue",
    ):
        response = req(h, "GET", path)
        assert response.status_code == 200, (path, response.text)
    session = req(h, "GET", "/session/saved").json()["data"]
    assert session["workspace"]["status"] == "missing"
    assert session["location"]["directory"] == str(h.root)
    for changes in ({"title": "Still readable"}, {"pinned": True}, {"archived": True}):
        assert req(h, "PATCH", "/session/saved", json=changes).status_code == 200
    assert h.store.load("saved").conversation_entries == h.record.conversation_entries
    assert not h.root.exists()
    assert not h.calls and not h.roots
    assert req(h, "GET", "/location/recent").json()["data"] == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/session/saved/selection", {"provider": "test", "model": "next"}),
        ("POST", "/session/saved/compact", {}),
        ("POST", "/session/saved/title/regenerate", {}),
        (
            "POST",
            "/session/saved/prompt",
            {"prompt": {"text": "cannot run"}, "resume": True},
        ),
        ("GET", "/tool?sessionID=saved", None),
        ("GET", "/session/saved/skill", None),
        ("GET", "/session/saved/mcp", None),
        ("PATCH", "/session/saved/tool", {"disabled": ["command_exec"]}),
    ],
)
def test_root_dependent_endpoints_return_one_actionable_error_without_provider_work(
    harness, method, path, body
):
    h = harness
    h.root.rmdir()
    original = (h.store.directory / "saved.jsonl").read_bytes()
    response = req(h, method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "session_workspace_unavailable"
    assert error["details"] == {
        "sessionID": "saved",
        "directory": str(h.root),
        "status": "missing",
    }
    assert error["requestID"]
    assert not h.roots and not h.calls
    assert (h.store.directory / "saved.jsonl").read_bytes() == original
    assert not h.root.exists()


@pytest.mark.parametrize("kind", ["missing", "file", "empty", "nul"])
def test_new_sessions_reject_invalid_directories_before_persistence(harness, kind):
    h = harness
    path = h.target / kind
    if kind == "file":
        path.write_text("not a directory")
    directory = "" if kind == "empty" else "\0bad" if kind == "nul" else str(path)
    response = req(
        h,
        "POST",
        "/session",
        json={"id": "invalid", "location": {"directory": directory}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "workspace_unavailable"
    assert not h.store.exists("invalid")


def test_relocation_preserves_identity_history_and_rejects_stale_or_invalid_targets(
    harness,
):
    h = harness
    h.root.rmdir()
    before = (h.store.directory / "saved.jsonl").read_bytes()
    moved = relocate(h, expectedDirectory=str(h.root))
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["workspace"]["status"] == "available"
    assert moved.json()["data"]["id"] == "saved"
    assert h.store.load("saved").conversation_entries == h.record.conversation_entries
    after = (h.store.directory / "saved.jsonl").read_bytes()
    assert after.startswith(before)
    assert relocate(h, expectedDirectory=str(h.root)).status_code == 200
    assert (h.store.directory / "saved.jsonl").read_bytes() == after
    third = h.target / "third"
    third.mkdir()
    stale = req(
        h,
        "POST",
        "/session/saved/relocate",
        json={"directory": str(third), "expectedDirectory": str(h.root)},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "session_workspace_conflict"
    assert h.store.load("saved").root == str(h.target)
    assert run_prompt(h)["state"] == "idle"
    assert h.roots[-1] == str(h.target)
    assert not h.root.exists()


def test_warm_runtime_relocation_closes_and_rediscovers_configuration(harness):
    h = harness
    (h.root / "AGENTS.md").write_text("ORIGINAL_WORKSPACE_INSTRUCTION")
    (h.target / "AGENTS.md").write_text("RELOCATED_WORKSPACE_INSTRUCTION")
    assert run_prompt(h)["state"] == "idle"
    runtime = h.app.state.runtime_registry.get_if_loaded("saved")
    original = runtime.resources.primary_locked()
    assert original is not None
    old_provider = original.provider
    (h.root / "AGENTS.md").unlink()
    h.root.rmdir()
    moved = eventually_relocate(h)
    assert moved.status_code == 200, moved.text
    assert original.closed
    assert id(old_provider) in h.closed
    assert run_prompt(h, "after relocation")["state"] == "idle"
    primary = runtime.resources.primary_locked()
    assert primary is not original
    assert primary._tool_root == h.target
    latest = "\n".join(h.calls[-1])
    assert "RELOCATED_WORKSPACE_INSTRUCTION" in latest
    assert "ORIGINAL_WORKSPACE_INSTRUCTION" not in latest


def test_fork_recovery_supports_a_new_target_without_poisoning_the_source(harness):
    h = harness
    h.root.rmdir()
    rejected = req(h, "POST", "/session/saved/fork", json={"id": "broken-fork"})
    assert rejected.status_code == 409
    assert not h.store.exists("broken-fork")
    response = req(
        h,
        "POST",
        "/session/saved/fork",
        json={"id": "rescued", "location": {"directory": str(h.target)}},
    )
    assert response.status_code == 200, response.text
    fork = h.store.load("rescued")
    assert fork.root == str(h.target)
    assert fork.conversation_entries == h.record.conversation_entries
    assert h.store.load("saved").root == str(h.root)


def test_missing_workspace_does_not_consume_queued_work_and_removal_allows_relocation(
    harness,
):
    h = harness
    response = req(
        h,
        "POST",
        "/session/saved/prompt",
        json={"id": "pending", "prompt": {"text": "keep this"}, "resume": False},
    )
    assert response.status_code == 200
    h.root.rmdir()
    queue = req(h, "GET", "/session/saved/queue").json()["data"]
    assert relocate(h).status_code == 409
    assert req(h, "GET", "/session/saved/queue").json()["data"] == queue
    removed = req(
        h,
        "PATCH",
        "/session/saved/queue",
        json={
            "expectedRevision": queue["revision"],
            "operations": [{"op": "remove", "id": "pending"}],
        },
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["items"] == []
    assert relocate(h).status_code == 200


def test_deletion_after_admission_pauses_the_same_input_instead_of_losing_it(
    harness, monkeypatch
):
    h = harness
    started, release = Event(), Event()
    original = config_runtime._build_startup_provider

    def blocked(args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(args, **kwargs)

    monkeypatch.setattr(config_runtime, "_build_startup_provider", blocked)
    try:
        response = req(
            h,
            "POST",
            "/session/saved/prompt",
            json={"id": "race", "prompt": {"text": "retained"}, "resume": True},
        )
        assert response.status_code == 200
        assert started.wait(2)
        h.root.rmdir()
    finally:
        release.set()
    waited = req(h, "POST", "/session/saved/wait", params={"timeoutMs": 3000}).json()[
        "data"
    ]
    assert waited["state"] == "error"
    assert "workspace" in waited["lastError"].lower()
    queue = req(h, "GET", "/session/saved/queue").json()["data"]
    assert [(item["id"], item["paused"]) for item in queue["items"]] == [("race", True)]
    assert h.store.load("saved").conversation_entries == h.record.conversation_entries
    assert not h.calls and not h.root.exists()


def test_retired_worker_keeps_relocation_excluded_until_physical_completion(harness):
    h = harness
    started, release = Event(), Event()
    h.gate[0] = (started, release)
    try:
        admitted = req(
            h,
            "POST",
            "/session/saved/prompt",
            json={"prompt": {"text": "block"}, "resume": True},
        )
        assert admitted.status_code == 200
        assert started.wait(2)
        assert req(h, "POST", "/session/saved/interrupt").status_code == 200
        assert relocate(h).status_code == 409
        with pytest.raises(WorkspaceBusy):
            relocate_session_workspace(h.store, "saved", h.target)
    finally:
        release.set()
    response = eventually_relocate(h)
    assert response.status_code == 200, response.text
