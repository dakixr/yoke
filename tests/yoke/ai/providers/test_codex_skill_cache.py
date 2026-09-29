# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.prompting import render_active_skill_message
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.ai.providers.codex.subscription import OAuthCredentials, convert_messages
from yoke.ai.providers.codex.subscription.messages import (
    codex_request_messages,
    convert_text_message,
    message_item,
)
from yoke.ai.providers.codex.websockets import CodexWebSockets, CodexWebSocketsConfig

IMAGE_URL = "data:image/png;base64," + ("a" * 64)


def _legacy_convert_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Projection before mid-session system context became developer input."""
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in codex_request_messages(messages):
        if message.role == "system":
            text = message.text_content()
            if text:
                instructions.append(text)
            continue
        if message.role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.text_content() or "",
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            text = message.text_content()
            if text:
                input_items.append(message_item(message.role, text))
            input_items.extend(
                {
                    "type": "function_call",
                    "call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                }
                for tool_call in message.tool_calls
            )
            continue
        input_items.append(convert_text_message(message))
    return "\n\n".join(instructions), input_items


def _tool_call(call_id: str, name: str = "read") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolFunction(name=name, arguments='{"path":"README.md"}'),
    )


def _calls(text: str, *call_ids: str, name: str = "read") -> Message:
    return Message(
        role="assistant",
        content=text,
        tool_calls=[_tool_call(call_id, name) for call_id in call_ids],
    )


def _skill(tmp_path: Path, name: str) -> SkillSpec:
    root = tmp_path / name
    root.mkdir()
    skill_md = root / "SKILL.md"
    skill_md.write_text(f"instructions for {name}", encoding="utf-8")
    return SkillSpec(
        name=name,
        description=f"Use {name}.",
        root=root,
        skill_md_path=skill_md,
    )


LEADING_ONLY_HISTORIES: dict[str, list[Message]] = {
    "plain": [
        Message.system("base"),
        Message.system("Available skills:\n- review: Use review."),
        Message.user("hello"),
        Message.assistant("hi"),
    ],
    "tools_and_images": [
        Message.system("base"),
        Message.user(
            [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
            ]
        ),
        _calls("reading", "a", "b"),
        Message.tool("a", '{"ok":true}'),
        Message.tool("b", '{"ok":false}'),
        Message.assistant("done"),
    ],
    "orphan_and_incomplete_tools": [
        Message.system("base"),
        Message.user("go"),
        Message.tool("orphan", "{}"),
        _calls("", "open"),
        Message.user("interrupt"),
    ],
    "compaction_hoisted_skill": [
        Message.system("base"),
        Message.system("Active skill:\nname: review\n\ninstructions for review"),
        Message.user("retained request"),
        Message.assistant("Compaction summary"),
        Message.user("next"),
    ],
}


@pytest.mark.parametrize("name", sorted(LEADING_ONLY_HISTORIES))
def test_projection_without_mid_session_system_context_is_unchanged(
    name: str,
) -> None:
    messages = LEADING_ONLY_HISTORIES[name]

    assert convert_messages(messages) == _legacy_convert_messages(messages)


def test_mid_session_skill_extends_input_without_changing_instructions() -> None:
    before = [
        Message.system("base"),
        Message.user("use the skill"),
        _calls("", "call-1", name="skill"),
        Message.tool("call-1", '{"loaded":["review"]}'),
    ]
    skill_text = "Active skill:\nname: review\n\ninstructions for review"
    after = [*before, Message.system(skill_text), Message.user("thanks")]

    before_instructions, before_items = convert_messages(before)
    after_instructions, after_items = convert_messages(after)

    assert after_instructions == before_instructions == "base"
    assert after_items[: len(before_items)] == before_items
    assert after_items[len(before_items) :] == [
        {"role": "developer", "content": [{"type": "input_text", "text": skill_text}]},
        {"role": "user", "content": [{"type": "input_text", "text": "thanks"}]},
    ]


class _ScriptedWebSocket:
    def __init__(self, sent: list[dict[str, Any]], replies: list[str]) -> None:
        self._sent = sent
        self._replies = replies
        self._events: list[str] = []

    def send(self, payload: str) -> None:
        self._sent.append(json.loads(payload))
        response_id = f"resp-{len(self._sent)}"
        text = self._replies[len(self._sent) - 1]
        self._events = [
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {"id": response_id, "usage": {}},
                }
            ),
        ]

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        return self._events.pop(0)

    def close(self) -> None:
        return None


def test_skill_mention_on_later_turn_continues_previous_response(
    tmp_path: Path,
) -> None:
    sent: list[dict[str, Any]] = []
    socket = _ScriptedWebSocket(sent, ["hi", "reviewed"])
    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=lambda url, **kwargs: socket,
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    registry = SkillRegistry([_skill(tmp_path, "review")])
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )

    try:
        agent.run("hello")
        agent.run("$review this change")
    finally:
        agent.close()

    first, second = sent
    assert second["instructions"] == first["instructions"]
    assert second["previous_response_id"] == "resp-1"
    skill_text = render_active_skill_message(registry.activate("review")).text_content()
    assert second["input"] == [
        {"role": "developer", "content": [{"type": "input_text", "text": skill_text}]},
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "$review this change"}],
        },
    ]
