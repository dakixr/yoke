from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

from pathlib import Path

from fastapi.testclient import TestClient

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.tools import LocalTool
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.session import SessionRecord
from yoke.session import SessionStore


TOKEN = "runtime-persistence-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class CapturingProvider:
    supports_image_inputs = True

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.close_count = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del tools
        self.requests.append([message.model_dump(mode="json") for message in messages])
        return Message.assistant("new answer")

    def close(self) -> None:
        self.close_count += 1


class InspectTool(LocalTool):
    name = "inspect"
    description = "Inspect a test value."
    execute_in_process = True

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def _run_saved_turn(
    tmp_path: Path,
    messages: list[Message],
    *,
    tools: list[LocalTool] | None = None,
    preserve_leaf: bool = False,
) -> tuple[SessionStore, CapturingProvider, dict[str, object]]:
    store = SessionStore(tmp_path / "sessions")
    if preserve_leaf:
        tree = SessionTree.from_messages(messages)
        exported = tree.export_for_persistence()
        record = store.save(
            "session-a",
            [],
            conversation_entries=[exported.entries[0]],
            leaf_id=exported.entries[0].id,
            root=tmp_path,
            title="Saved title",
        )
        store.save_indexed_tree_navigation(
            "session-a",
            existing_record=record,
            leaf_id=exported.entries[-1].id,
            appended_entries=tuple(exported.entries[1:]),
            clear_context_usage=False,
        )
    else:
        store.save(
            "session-a",
            messages,
            root=tmp_path,
            title="Saved title",
        )
    provider = CapturingProvider()

    def factory(_record: SessionRecord) -> RuntimeAgent:
        return RuntimeAgent(provider=provider, tools=tools or [])

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=store.directory,
            agent_factory=factory,
        )
    )
    with TestClient(app) as client:
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={
                "id": "input-a",
                "prompt": {"text": "new question"},
                "resume": True,
            },
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        wait_data = waited.json()["data"]
    return store, provider, wait_data


def test_instruction_in_saved_path_uses_normalized_append_cursor(
    tmp_path: Path,
) -> None:
    store, provider, wait_data = _run_saved_turn(
        tmp_path,
        [
            Message.system("saved instruction"),
            Message.user("old question"),
            Message.assistant("old answer"),
        ],
    )

    assert wait_data["state"] == "idle"
    assert provider.requests == [
        [
            Message.user("old question").model_dump(mode="json"),
            Message.assistant("old answer").model_dump(mode="json"),
            Message.user("new question").model_dump(mode="json"),
        ]
    ]
    persisted = store.load("session-a")
    assert [entry.kind for entry in persisted.conversation_entries] == [
        "instruction",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert persisted.conversation_entries[0].message == Message.system(
        "saved instruction"
    )
    assert [message.plain_text_content for message in persisted.messages[-2:]] == [
        "new question",
        "new answer",
    ]


def test_instruction_leaf_is_preserved_and_reanchored_after_provider_call(
    tmp_path: Path,
) -> None:
    store, provider, wait_data = _run_saved_turn(
        tmp_path,
        [Message.user("old question"), Message.system("leaf instruction")],
    )

    assert wait_data["state"] == "idle"
    assert provider.requests == [
        [
            Message.user("old question").model_dump(mode="json"),
            Message.user("new question").model_dump(mode="json"),
        ]
    ]
    persisted = store.load("session-a")
    active = {entry.id: entry for entry in persisted.conversation_entries}
    assert [entry.kind for entry in persisted.conversation_entries] == [
        "user",
        "instruction",
        "user",
        "assistant",
    ]
    leaf_instruction = persisted.conversation_entries[1]
    new_user = persisted.conversation_entries[2]
    assert new_user.parent_id == leaf_instruction.id
    assert active[persisted.leaf_id or ""].kind == "assistant"


def test_dangling_tool_recovery_remains_in_append_suffix(tmp_path: Path) -> None:
    call = ToolCall(
        id="unfinished-call",
        function=ToolFunction(name="inspect", arguments="{}"),
    )
    store, provider, wait_data = _run_saved_turn(
        tmp_path,
        [
            Message.user("inspect this"),
            Message(role="assistant", content=None, tool_calls=[call]),
        ],
        tools=[InspectTool()],
        preserve_leaf=True,
    )

    assert wait_data["state"] == "idle"
    request = provider.requests[0]
    assert [message["role"] for message in request] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert request[2]["tool_call_id"] == "unfinished-call"
    assert request[2]["content"] == (
        '{"ok":false,"cancelled":true,"error":'
        '"Tool call was incomplete when the session resumed."}'
    )
    persisted = store.load("session-a")
    assert [entry.kind for entry in persisted.conversation_entries] == [
        "user",
        "assistant_tool_calls",
        "tool_result",
        "user",
        "assistant",
    ]
    recovered = persisted.conversation_entries[2].message
    assert recovered is not None
    assert recovered.tool_call_id == "unfinished-call"
    assert recovered.content == (
        '{"ok":false,"cancelled":true,"error":'
        '"Tool call was incomplete when the session resumed."}'
    )


def _run_combined_projection(
    tmp_path: Path,
    *,
    indexed: bool,
) -> list[dict[str, object]]:
    directory = tmp_path / ("indexed" if indexed else "fallback")
    store = SessionStore(directory)
    image = Message.user(
        [
            MessageTextContentPart(text="inspect retained image"),
            MessageImageURLContentPart(
                image_url=MessageImageURL(url="data:image/png;base64,COMBINED"),
                label="combined image",
            ),
        ]
    )
    tree = SessionTree.from_messages(
        [
            Message.system("saved instruction"),
            Message.user("root request"),
            Message.assistant("root answer"),
        ]
    )
    branch_point = tree.current
    assert branch_point is not None
    tree.append_system_event(Message.system("abandoned skill instructions"))
    tree.append_message(Message.assistant("abandoned branch answer"))
    tree.checkout(branch_point)
    tree.append_system_event(Message.system("active branch instructions"))
    tree.append_message(image)
    tree.append_checkpoint(
        "checkpoint summary",
        retained_messages=[image, Message.user("retained checkpoint request")],
    )
    dangling = ToolCall(
        id="combined-dangling",
        function=ToolFunction(name="inspect", arguments="{}"),
    )
    tree.append_message(Message.user("dangling tool request"))
    tree.append_message(Message(role="assistant", content=None, tool_calls=[dangling]))
    exported = tree.export_for_persistence()
    skill = ActiveSkill(
        name="review",
        description="Review carefully.",
        source_path="<inline>",
        content="active configured skill",
    )
    store.save(
        "session-a",
        [],
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        active_skills=[skill],
        root=tmp_path,
        title="Combined projection",
    )
    provider = CapturingProvider()

    def factory(_record: SessionRecord) -> RuntimeAgent:
        return RuntimeAgent(
            provider,
            [InspectTool.bind()],
            context_manager=ContextManager(
                instructions=[
                    Message.system("configured system instruction"),
                    Message.system("configured tool instruction"),
                ]
            ),
            active_skills=[skill],
        )

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=directory,
            agent_factory=factory,
        )
    )
    app.state.runtime_registry.indexed_runtime_seed = indexed
    if indexed:
        index = app.state.session_service.message_index
        assert index._ensure("session-a") is not None
        assert index.runtime_seed("session-a") is not None
    with TestClient(app) as client:
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "new branch request"}, "resume": True},
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.json()["data"]["state"] == "idle"
    assert len(provider.requests) == 1
    return provider.requests[0]


def test_combined_indexed_seed_preserves_full_provider_projection(
    tmp_path: Path,
) -> None:
    fallback = _run_combined_projection(tmp_path, indexed=False)
    indexed = _run_combined_projection(tmp_path, indexed=True)

    assert indexed == fallback
    serialized = str(indexed)
    assert "configured system instruction" in serialized
    assert "configured tool instruction" in serialized
    assert "active configured skill" in serialized
    assert "checkpoint summary" in serialized
    assert "COMBINED" in serialized
    assert "new branch request" in serialized
    assert "dangling tool request" in serialized
    assert "abandoned branch answer" not in serialized
    assert "abandoned skill instructions" not in serialized
