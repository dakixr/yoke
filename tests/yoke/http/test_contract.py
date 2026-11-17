from __future__ import annotations

# ruff: noqa: D100,D103,S101
from collections.abc import Callable, Iterator
from contextlib import ExitStack
import json
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.agent.models import (
    Message,
)
from yoke.agent.session_tree import SessionTree
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.resolution import ProviderReadiness
from yoke.http.app import HttpAppSettings, create_app

TOKEN = "test-secret"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            HttpAppSettings(
                auth_token=TOKEN,
                session_directory=tmp_path / "sessions",
            )
        )
    )


@pytest.fixture
def clients() -> Iterator[Callable[..., TestClient]]:
    with ExitStack() as stack:

        def open_client(root: Path) -> TestClient:
            return stack.enter_context(_client(root))

        yield open_client


@pytest.fixture(scope="module")
def openapi_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    # Schema checks are read-only and share both route compilation and OpenAPI.
    with _client(tmp_path_factory.mktemp("openapi")) as client:
        yield client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _fastapi(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _save_conversation(
    client: TestClient, root: Path, session_id: str
) -> tuple[str, str]:
    assistant = Message.assistant("world")
    assistant.reasoning_content = "private reasoning"
    assistant.reasoning_signature = "private signature"
    messages = [Message.user("hello"), assistant]
    tree = SessionTree.from_messages(messages)
    exported = tree.export_for_persistence()
    state = getattr(client.app, "state")
    state.session_service.store.save(
        session_id,
        messages,
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
        title="Conversation",
    )
    return exported.entries[0].id, exported.entries[-1].id


def test_health_is_public_and_capabilities_require_auth(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["data"]["protocolVersion"] == "1"

    denied = client.get("/api/v1/capabilities")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert denied.json()["error"]["code"] == "unauthorized"
    assert denied.json()["error"]["requestID"].startswith("req_")

    allowed = client.get("/api/v1/capabilities", headers=_auth())
    assert allowed.status_code == 200
    assert allowed.json()["data"]["features"]["sessionTree"] is True
    assert allowed.json()["data"]["features"]["promptAdmission"] is True
    assert allowed.json()["data"]["features"]["globalEvents"] is True
    assert allowed.json()["data"]["features"]["queueEditor"] is True
    assert allowed.json()["data"]["features"]["permissions"] is True
    assert allowed.json()["data"]["features"]["questions"] is True


def test_session_create_is_idempotent_and_location_conflicts(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    body = {
        "id": "session-a",
        "location": {"directory": str(root)},
        "title": "First",
        "selection": {
            "provider": "openai",
            "model": "model-a",
            "reasoningEffort": "high",
        },
    }

    created = client.post("/api/v1/session", headers=_auth(), json=body)
    repeated = client.post("/api/v1/session", headers=_auth(), json=body)

    assert created.status_code == 200
    assert repeated.status_code == 200
    assert created.json() == repeated.json()
    assert created.json()["data"]["selection"]["reasoningEffort"] == "high"
    assert created.json()["data"]["tree"]["entryCount"] == 0

    conflicting = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={**body, "location": {"directory": str(other)}},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "session_identity_conflict"


def test_location_browse_expands_home_and_rejects_relative_paths(
    clients,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = clients(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "dev").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    browsed = client.get(
        "/api/v1/location/browse",
        headers=_auth(),
        params={"path": "~/d"},
    )
    assert browsed.status_code == 200
    assert browsed.json()["data"]["entries"] == [
        {"name": "dev", "directory": str((home / "dev").resolve())}
    ]

    relative = client.get(
        "/api/v1/location/browse",
        headers=_auth(),
        params={"path": "./dev"},
    )
    assert relative.status_code == 400
    assert relative.json()["error"]["code"] == "location_path_not_absolute"


def test_session_archive_is_durable_filterable_and_reopenable(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    for session_id in ("session-a", "session-b"):
        created = client.post(
            "/api/v1/session",
            headers=_auth(),
            json={"id": session_id, "location": {"directory": str(root)}},
        )
        assert created.status_code == 200

    archived = client.patch(
        "/api/v1/session/session-a",
        headers=_auth(),
        json={"archived": True},
    )
    assert archived.status_code == 200
    archived_at = archived.json()["data"]["archivedAt"]
    assert archived_at

    active_list = client.get(
        "/api/v1/session",
        headers=_auth(),
        params={"archived": False},
    )
    assert [item["id"] for item in active_list.json()["data"]] == ["session-b"]
    archived_list = client.get(
        "/api/v1/session",
        headers=_auth(),
        params={"archived": True},
    )
    assert [item["id"] for item in archived_list.json()["data"]] == ["session-a"]

    restarted = clients(tmp_path)
    persisted = restarted.get("/api/v1/session/session-a", headers=_auth())
    assert persisted.status_code == 200
    assert persisted.json()["data"]["archivedAt"] == archived_at

    reopened = restarted.patch(
        "/api/v1/session/session-a",
        headers=_auth(),
        json={"archived": False},
    )
    assert reopened.status_code == 200
    assert reopened.json()["data"]["archivedAt"] is None


def test_message_and_tree_projection_strip_provider_private_fields(
    clients,
    tmp_path: Path,
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    user_id, assistant_id = _save_conversation(client, root, "session-a")

    messages = client.get(
        "/api/v1/session/session-a/message",
        headers=_auth(),
        params={"order": "asc"},
    )
    assert messages.status_code == 200
    payload = messages.json()
    assert [item["type"] for item in payload["data"]] == ["user", "assistant"]
    assert [item["id"] for item in payload["data"]] == [user_id, assistant_id]
    raw = messages.text
    assert "private reasoning" not in raw
    assert "private signature" not in raw
    assert "reasoningSignature" not in raw

    tree = client.get("/api/v1/session/session-a/tree", headers=_auth())
    assert tree.status_code == 200
    assert tree.json()["data"]["leafID"] == assistant_id
    assert tree.json()["data"]["entries"][-1]["current"] is True
    assert tree.json()["data"]["entries"][-1]["active"] is True


@pytest.mark.xdist_group("openapi")
def test_openapi_uses_api_v1_and_camel_case_fields(openapi_client: TestClient) -> None:
    client = openapi_client
    schema = client.get("/api/v1/openapi.json")

    assert schema.status_code == 200
    payload = schema.json()
    assert "/api/v1/session" in payload["paths"]
    selection = payload["components"]["schemas"]["SessionSelection"]
    assert "reasoningEffort" in selection["properties"]
    assert "reasoning_effort" not in selection["properties"]


@pytest.mark.xdist_group("openapi")
def test_openapi_matches_checked_in_golden_and_operation_ids_are_unique(
    openapi_client: TestClient,
) -> None:
    client = openapi_client
    payload = _fastapi(client).openapi()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    golden = Path(__file__).with_name("golden") / "openapi.json"
    assert rendered == golden.read_text(encoding="utf-8")

    operation_ids = [
        operation["operationId"]
        for path in payload["paths"].values()
        for method, operation in path.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    ]
    assert operation_ids
    assert len(operation_ids) == len(set(operation_ids))


def test_provider_and_model_catalogs_are_typed_and_searchable(
    clients,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yoke.http.services.catalog_service.list_provider_readiness",
        lambda **_kwargs: [
            ProviderReadiness(
                provider_name="demo",
                ready=True,
                model="model-a",
                reasoning_effort="medium",
            )
        ],
    )
    monkeypatch.setattr(
        "yoke.http.services.catalog_service.available_provider_names",
        lambda **_kwargs: ["demo"],
    )
    monkeypatch.setattr(
        "yoke.http.services.catalog_service.list_provider_models",
        lambda _provider, **_kwargs: [
            ProviderModelInfo(
                id="model-a",
                display_name="Model A",
                context_window_tokens=123_456,
                thinking_levels=("low", "medium", "high"),
                supports_image_inputs=True,
            )
        ],
    )
    client = clients(tmp_path)

    providers = client.get("/api/v1/provider", headers=_auth())
    assert providers.status_code == 200
    assert providers.json()["data"] == [
        {
            "id": "demo",
            "ready": True,
            "reason": None,
            "currentModel": "model-a",
            "currentReasoningEffort": "medium",
        }
    ]

    models = client.get(
        "/api/v1/model",
        headers=_auth(),
        params={"search": "Model A"},
    )
    assert models.status_code == 200
    assert models.json()["data"] == [
        {
            "id": "model-a",
            "provider": "demo",
            "name": "Model A",
            "reasoningEfforts": ["low", "medium", "high"],
            "defaultReasoningEffort": "medium",
            "capabilities": {"images": True, "tools": True},
            "contextWindowTokens": 123_456,
        }
    ]


def test_skill_catalog_and_activation_persist_tree_state(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    skill_dir = root / ".yoke" / "skills" / "reviewer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review changes carefully.\n---\n"
        "Review the current changes and report concrete problems.\n",
        encoding="utf-8",
    )
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "skill-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200

    catalog = client.get(
        "/api/v1/skill",
        headers=_auth(),
        params={"directory": str(root), "search": "reviewer"},
    )
    assert catalog.status_code == 200
    assert catalog.json()["data"][0]["name"] == "reviewer"

    activated = client.post(
        "/api/v1/session/skill-session/skill/reviewer/activate",
        headers=_auth(),
        json={},
    )
    assert activated.status_code == 200
    assert activated.json()["data"]["activated"]["active"] is True
    assert activated.json()["data"]["promptInputID"] is None

    session_skills = client.get(
        "/api/v1/session/skill-session/skill",
        headers=_auth(),
    ).json()["data"]
    assert [item["name"] for item in session_skills["active"]] == ["reviewer"]
    assert (
        next(
            item for item in session_skills["available"] if item["name"] == "reviewer"
        )["active"]
        is True
    )

    tree = client.get(
        "/api/v1/session/skill-session/tree",
        headers=_auth(),
    ).json()["data"]
    assert tree["entries"][-1]["kind"] == "skill_event"


def test_filesystem_routes_are_location_contained(clients, tmp_path: Path) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    nested = root / "src"
    nested.mkdir(parents=True)
    (nested / "hello.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
        newline="\n",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)

    listed = client.get(
        "/api/v1/fs/list",
        headers=_auth(),
        params={"directory": str(root)},
    )
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()["data"]]
    assert "src" in names
    assert "escape.txt" not in names

    found = client.get(
        "/api/v1/fs/find",
        headers=_auth(),
        params={"directory": str(root), "query": "hello", "type": "file"},
    )
    assert found.status_code == 200
    assert found.json()["data"][0]["path"] == "src/hello.py"

    read = client.get(
        "/api/v1/fs/read",
        headers=_auth(),
        params={"directory": str(root), "path": "src/hello.py"},
    )
    assert read.status_code == 200
    assert read.text == "print('hello')\n"

    traversal = client.get(
        "/api/v1/fs/read",
        headers=_auth(),
        params={"directory": str(root), "path": "../outside.txt"},
    )
    assert traversal.status_code == 403
    assert traversal.json()["error"]["code"] == "path_outside_location"

    symlink_escape = client.get(
        "/api/v1/fs/read",
        headers=_auth(),
        params={"directory": str(root), "path": "escape.txt"},
    )
    assert symlink_escape.status_code == 403


def test_tool_inventory_and_session_override_do_not_require_provider_runtime(
    clients,
    tmp_path: Path,
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "tools-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200

    inventory = client.get(
        "/api/v1/tool",
        headers=_auth(),
        params={"sessionID": "tools-session"},
    )
    assert inventory.status_code == 200
    enabled_tools = [item for item in inventory.json()["data"] if item["enabled"]]
    assert enabled_tools
    target = enabled_tools[0]["name"]

    patched = client.patch(
        "/api/v1/session/tools-session/tool",
        headers=_auth(),
        json={"disabled": [target]},
    )
    assert patched.status_code == 200
    assert target not in patched.json()["data"]["enabled"]

    refreshed = client.get(
        "/api/v1/tool",
        headers=_auth(),
        params={"sessionID": "tools-session"},
    )
    target_row = next(
        item for item in refreshed.json()["data"] if item["name"] == target
    )
    assert target_row["enabled"] is False


def test_command_palette_metadata_exposes_typed_actions(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)
    response = client.get("/api/v1/command", headers=_auth())

    assert response.status_code == 200
    commands = {item["name"]: item for item in response.json()["data"]}
    assert commands["compact"]["action"] == "session.compact"
    assert commands["image"]["action"] == "upload.create"
    assert commands["skill"]["usage"] == "/skill <name> [prompt]"


def test_permission_and_question_routes_resolve_worker_waits(
    clients, tmp_path: Path
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "human-input-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200
    service = _fastapi(client).state.human_input_service

    permission = service.create_permission(
        "human-input-session",
        permission="shell.write",
        message="Allow writing outside the current file?",
    )
    listed_permissions = client.get(
        "/api/v1/session/human-input-session/permission",
        headers=_auth(),
    )
    assert listed_permissions.status_code == 200
    assert listed_permissions.json()["data"][0]["id"] == permission.id
    replied_permission = client.post(
        f"/api/v1/session/human-input-session/permission/{permission.id}/reply",
        headers=_auth(),
        json={"reply": "allow", "message": "approved in browser"},
    )
    assert replied_permission.status_code == 200
    permission_resolution = service.wait_permission(permission.id, 0.1)
    assert permission_resolution.reply == "allow"
    assert permission_resolution.message == "approved in browser"
    assert (
        client.get(
            "/api/v1/session/human-input-session/permission",
            headers=_auth(),
        ).json()["data"]
        == []
    )

    question = service.create_question(
        "human-input-session",
        question="Which branch?",
        options=["main", "feature"],
    )
    listed_questions = client.get(
        "/api/v1/session/human-input-session/question",
        headers=_auth(),
    )
    assert listed_questions.status_code == 200
    assert listed_questions.json()["data"][0]["options"] == ["main", "feature"]
    invalid = client.post(
        f"/api/v1/session/human-input-session/question/{question.id}/reply",
        headers=_auth(),
        json={"answers": ["unknown"]},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_question_reply"
    replied_question = client.post(
        f"/api/v1/session/human-input-session/question/{question.id}/reply",
        headers=_auth(),
        json={"answers": ["feature"]},
    )
    assert replied_question.status_code == 200
    question_resolution = service.wait_question(question.id, 0.1)
    assert question_resolution.answers == ["feature"]
    assert question_resolution.rejected is False


def test_mcp_repo_scope_persists_policy_and_explicit_null_clears_allowlist(
    clients,
    tmp_path: Path,
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    config_path = root / ".yoke" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "demo": {
                        "command": "python",
                        "enabled": True,
                        "enabled_tools": ["first", "second"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "mcp-repo-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200

    patched = client.patch(
        "/api/v1/session/mcp-repo-session/mcp/demo",
        headers=_auth(),
        json={
            "scope": "repo",
            "enabled": False,
            "disabledTools": ["blocked"],
        },
    )
    assert patched.status_code == 200
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    entry = persisted["mcp_servers"]["demo"]
    assert entry["enabled"] is False
    assert entry["disabled_tools"] == ["blocked"]
    assert entry["enabled_tools"] == ["first", "second"]

    cleared = client.patch(
        "/api/v1/session/mcp-repo-session/mcp/demo",
        headers=_auth(),
        json={"scope": "repo", "enabledTools": None},
    )
    assert cleared.status_code == 200
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "enabled_tools" not in persisted["mcp_servers"]["demo"]


def test_tool_trace_http_redacts_secrets_and_uses_output_sequence_cursor(
    clients,
    tmp_path: Path,
) -> None:
    client = clients(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "session-a", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200
    state = getattr(client.app, "state")
    runtime = state.runtime_registry.get_or_start("session-a")
    traces = runtime.tool_traces
    traces.record_event(
        "tool_execution_start",
        {
            "tool_call_id": "call-a",
            "tool_name": "demo",
            "tool_arguments": '{"api_key":"very-secret","path":"file.txt"}',
            "iteration": 1,
            "turn_id": 7,
        },
    )
    traces.record_event(
        "tool_execution_output_delta",
        {
            "tool_call_id": "call-a",
            "tool_name": "demo",
            "text": "one",
            "stream": "stdout",
            "turn_id": 7,
        },
    )
    traces.record_event(
        "tool_execution_output_delta",
        {
            "tool_call_id": "call-a",
            "tool_name": "demo",
            "text": "two",
            "stream": "stdout",
            "turn_id": 7,
        },
    )
    traces.record_event(
        "tool_execution_end",
        {
            "tool_call_id": "call-a",
            "tool_name": "demo",
            "executed_arguments": {"authorization": "Bearer nope", "path": "file.txt"},
            "result": {"ok": True, "password": "nope", "value": 3},
            "ok": True,
            "turn_id": 7,
        },
    )

    detail = client.get(
        "/api/v1/session/session-a/tool-call/call-a",
        headers=_auth(),
    )
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["retention"] == "runtime"
    assert "very-secret" not in detail.text
    assert "Bearer nope" not in detail.text
    assert '"api_key":"<redacted>"' in data["arguments"]["raw"]
    assert data["arguments"]["executed"]["authorization"] == "<redacted>"
    assert data["result"]["password"] == "<redacted>"
    assert data["output"]["latestSeq"] == 2

    output = client.get(
        "/api/v1/session/session-a/tool-call/call-a/output",
        headers=_auth(),
        params={"afterSeq": 1},
    )
    assert output.status_code == 200
    assert output.json()["data"] == [{"seq": 2, "stream": "stdout", "text": "two"}]
