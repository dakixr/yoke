from __future__ import annotations

# ruff: noqa: D100,D103,S101

import json
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.resolution import ProviderReadiness
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import write_prompt_queue_snapshot


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _fastapi(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _save_conversation(client: TestClient, root: Path, session_id: str) -> tuple[str, str]:
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


def test_health_is_public_and_capabilities_require_auth(tmp_path: Path) -> None:
    client = _client(tmp_path)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["data"]["protocolVersion"] == "1"

    denied = client.get("/api/v1/capabilities")
    assert denied.status_code == 401
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


def test_session_create_is_idempotent_and_location_conflicts(tmp_path: Path) -> None:
    client = _client(tmp_path)
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


def test_session_list_cursor_and_queue_summary(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    for session_id in ("session-a", "session-b", "session-c"):
        response = client.post(
            "/api/v1/session",
            headers=_auth(),
            json={"id": session_id, "location": {"directory": str(root)}},
        )
        assert response.status_code == 200

    state = getattr(client.app, "state")
    store = state.session_service.store
    write_prompt_queue_snapshot(
        store.directory,
        "session-b",
        PersistedPromptQueue(
            revision=7,
            prompts=[
                PersistedPendingInput(
                    id="inp-1",
                    prompt="steer",
                    kind="steering",
                    created_at="2026-08-26T12:00:00+00:00",
                ),
                PersistedPendingInput(
                    id="inp-2",
                    prompt="later",
                    kind="queued",
                    created_at="2026-08-26T12:01:00+00:00",
                    paused=True,
                ),
            ],
        ),
    )

    first = client.get(
        "/api/v1/session",
        headers=_auth(),
        params={"directory": str(root), "limit": 2, "order": "createdAsc"},
    )
    assert first.status_code == 200
    assert len(first.json()["data"]) == 2
    cursor = first.json()["cursor"]["next"]
    assert cursor

    second = client.get(
        "/api/v1/session",
        headers=_auth(),
        params={
            "directory": str(root),
            "limit": 2,
            "order": "createdAsc",
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert len(second.json()["data"]) == 1

    session_b = client.get("/api/v1/session/session-b", headers=_auth()).json()["data"]
    assert session_b["queue"] == {
        "total": 2,
        "steering": 1,
        "queued": 1,
        "paused": 1,
        "revision": 7,
    }


def test_message_and_tree_projection_strip_provider_private_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
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


def test_fork_from_entry_selects_requested_branch_without_mutating_source(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    user_id, assistant_id = _save_conversation(client, root, "source")

    forked = client.post(
        "/api/v1/session/source/fork",
        headers=_auth(),
        json={"id": "forked", "fromEntryID": user_id},
    )

    assert forked.status_code == 200
    assert forked.json()["data"]["tree"]["leafID"] == user_id
    source = client.get("/api/v1/session/source", headers=_auth()).json()["data"]
    assert source["tree"]["leafID"] == assistant_id


def test_tree_navigation_preserves_editor_handoff_and_revision_conflicts(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    messages = [
        Message.system("system"),
        Message.user("hello"),
        Message.assistant("world"),
    ]
    session_tree = SessionTree.from_messages(messages)
    exported = session_tree.export_for_persistence()
    state = getattr(client.app, "state")
    state.session_service.store.save(
        "session-a",
        messages,
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
        title="Conversation",
    )
    user_id = exported.entries[1].id
    assistant_id = exported.entries[2].id
    instruction_id = exported.entries[0].id
    tree = client.get("/api/v1/session/session-a/tree", headers=_auth()).json()["data"]
    revision = tree["revision"]

    preview = client.get(
        "/api/v1/session/session-a/tree/navigation-preview",
        headers=_auth(),
        params={"targetID": user_id},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["editorText"] == "hello"
    assert preview.json()["data"]["abandoned"][-1]["id"] == assistant_id

    labeled = client.patch(
        f"/api/v1/session/session-a/tree/{assistant_id}",
        headers=_auth(),
        json={"expectedRevision": revision, "label": " good checkpoint "},
    )
    assert labeled.status_code == 200
    labeled_data = labeled.json()["data"]
    assert labeled_data["entry"]["label"] == "good checkpoint"
    new_revision = labeled_data["revision"]

    stale = client.post(
        "/api/v1/session/session-a/tree/navigate",
        headers=_auth(),
        json={"expectedRevision": revision, "targetID": user_id},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "tree_revision_conflict"

    navigated = client.post(
        "/api/v1/session/session-a/tree/navigate",
        headers=_auth(),
        json={"expectedRevision": new_revision, "targetID": user_id},
    )
    assert navigated.status_code == 200
    assert navigated.json()["data"]["leafID"] == instruction_id
    assert navigated.json()["data"]["editorText"] == "hello"


def test_openapi_uses_api_v1_and_camel_case_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    schema = client.get("/api/v1/openapi.json")

    assert schema.status_code == 200
    payload = schema.json()
    assert "/api/v1/session" in payload["paths"]
    selection = payload["components"]["schemas"]["SessionSelection"]
    assert "reasoningEffort" in selection["properties"]
    assert "reasoning_effort" not in selection["properties"]


def test_openapi_matches_checked_in_golden_and_operation_ids_are_unique(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
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
    client = _client(tmp_path)

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
            "capabilities": {"images": True, "tools": True},
            "contextWindowTokens": 123_456,
        }
    ]


def test_skill_catalog_and_activation_persist_tree_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
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
    assert next(
        item for item in session_skills["available"] if item["name"] == "reviewer"
    )["active"] is True

    tree = client.get(
        "/api/v1/session/skill-session/tree",
        headers=_auth(),
    ).json()["data"]
    assert tree["entries"][-1]["kind"] == "skill_event"


def test_filesystem_routes_are_location_contained(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    nested = root / "src"
    nested.mkdir(parents=True)
    (nested / "hello.py").write_text("print('hello')\n", encoding="utf-8")
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
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
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


def test_command_palette_metadata_exposes_typed_actions(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/api/v1/command", headers=_auth())

    assert response.status_code == 200
    commands = {item["name"]: item for item in response.json()["data"]}
    assert commands["compact"]["action"] == "session.compact"
    assert commands["image"]["action"] == "upload.create"
    assert commands["skill"]["usage"] == "/skill <name> [prompt]"


def test_context_excludes_system_by_default_and_can_include_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    messages = [
        Message.system("sensitive system instruction"),
        Message.user("hello"),
        Message.assistant("world"),
    ]
    tree = SessionTree.from_messages(messages)
    exported = tree.export_for_persistence()
    _fastapi(client).state.session_service.store.save(
        "context-session",
        messages,
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        root=root,
    )

    default = client.get(
        "/api/v1/session/context-session/context",
        headers=_auth(),
    )
    assert default.status_code == 200
    default_text = default.text
    assert "sensitive system instruction" not in default_text
    assert "hello" in default_text

    privileged = client.get(
        "/api/v1/session/context-session/context",
        headers=_auth(),
        params={"includeSystem": True},
    )
    assert privileged.status_code == 200
    assert "sensitive system instruction" in privileged.text


def test_permission_and_question_routes_resolve_worker_waits(tmp_path: Path) -> None:
    client = _client(tmp_path)
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
    assert client.get(
        "/api/v1/session/human-input-session/permission",
        headers=_auth(),
    ).json()["data"] == []

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


def test_mcp_session_policy_is_typed_and_does_not_expose_secrets(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "repo"
    config_dir = root / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.json").write_text(
        """{
  "mcp_servers": {
    "demo": {
      "command": "python",
      "args": ["-c", "print('secret command')"],
      "env": {"API_KEY": "top-secret"},
      "enabled": true,
      "disabled_tools": ["hidden"]
    }
  }
}
""",
        encoding="utf-8",
    )
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "mcp-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200

    listed = client.get(
        "/api/v1/session/mcp-session/mcp",
        headers=_auth(),
    )
    assert listed.status_code == 200
    server = listed.json()["data"][0]
    assert server["name"] == "demo"
    assert server["enabled"] is True
    assert server["scope"] == "repo"
    assert server["disabledTools"] == ["hidden"]
    assert "top-secret" not in listed.text
    assert "secret command" not in listed.text

    patched = client.patch(
        "/api/v1/session/mcp-session/mcp/demo",
        headers=_auth(),
        json={"enabled": False, "disabledTools": ["hidden", "other"]},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["enabled"] is False
    assert patched.json()["data"]["disabledTools"] == ["hidden", "other"]

    refreshed = client.get(
        "/api/v1/session/mcp-session/mcp",
        headers=_auth(),
    )
    assert refreshed.json()["data"][0]["enabled"] is False


def test_mcp_repo_scope_persists_policy_and_explicit_null_clears_allowlist(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
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


def test_mcp_global_scope_uses_injected_home_and_does_not_touch_real_home(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    home = tmp_path / "home"
    global_path = home / ".yoke" / "mcp.json"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "global-demo": {
                        "command": "python",
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _fastapi(client).state.mcp_service.home = home.resolve()
    root = tmp_path / "repo"
    root.mkdir()
    created = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": "mcp-global-session", "location": {"directory": str(root)}},
    )
    assert created.status_code == 200

    listed = client.get(
        "/api/v1/session/mcp-global-session/mcp",
        headers=_auth(),
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["scope"] == "global"

    patched = client.patch(
        "/api/v1/session/mcp-global-session/mcp/global-demo",
        headers=_auth(),
        json={"scope": "global", "enabled": False},
    )
    assert patched.status_code == 200
    persisted = json.loads(global_path.read_text(encoding="utf-8"))
    assert persisted["mcp_servers"]["global-demo"]["enabled"] is False


def test_tool_trace_http_redacts_secrets_and_uses_output_sequence_cursor(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
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
    traces = runtime.tool_trace_store()
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
    assert output.json()["data"] == [
        {"seq": 2, "stream": "stdout", "text": "two"}
    ]
