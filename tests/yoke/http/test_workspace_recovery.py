from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from yoke.agent.models import Message
from yoke.cli.config import runtime as config_runtime
from yoke.http.app import HttpAppSettings, create_app
from tests.yoke.session_rows import stored_entry_bytes, stored_session_bytes


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


def test_relocation_preserves_identity_history_and_rejects_stale_or_invalid_targets(
    harness,
):
    h = harness
    h.root.rmdir()
    history = stored_entry_bytes(h.store, "saved")
    moved = relocate(h, expectedDirectory=str(h.root))
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["workspace"]["status"] == "available"
    assert moved.json()["data"]["id"] == "saved"
    assert h.store.load("saved").conversation_entries == h.record.conversation_entries
    after = stored_session_bytes(h.store, "saved")
    assert stored_entry_bytes(h.store, "saved") == history
    assert relocate(h, expectedDirectory=str(h.root)).status_code == 200
    assert stored_session_bytes(h.store, "saved") == after
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
