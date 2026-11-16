"""Normalization must retain cursor positions and arbitrary message content."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

from yoke.agent.models import Message

from .golden import Normalizer
from .recording import tool_calls


def test_only_command_process_identity_is_normalized(tmp_path: Path) -> None:
    cursor = (
        "pc1_" + base64.urlsafe_b64encode(struct.pack(">QQQ", 897123, 7, 9)).decode()
    )
    command = tool_calls(("command_exec", {"argv": ["/bin/echo", "oracle"]}))
    result = Message.tool(
        "call-1-command_exec",
        json.dumps(
            {
                "session_id": 897123,
                "cursor": cursor,
                "output": f'exact  text\n{cursor}, "session_id": 897123',
            }
        ),
    )
    ordinary = Message.user(f'Literal content: {cursor}, "session_id": 897123')
    calls = [
        {
            "kind": "model_iteration",
            "messages_json": [m.model_dump_json() for m in (ordinary, command, result)],
        }
    ]

    normalized = Normalizer(tmp_path).normalize(calls)

    assert normalized[0]["messages_json"][0] == ordinary.model_dump_json()
    payload = json.loads(json.loads(normalized[0]["messages_json"][-1])["content"])
    assert payload["session_id"] == 1
    assert struct.unpack(">QQQ", base64.urlsafe_b64decode(payload["cursor"][4:])) == (
        1,
        7,
        9,
    )
    assert payload["output"] == f'exact  text\n{cursor}, "session_id": 897123'
    assert isinstance(result.content, str)
    changed_cursor = (
        "pc1_" + base64.urlsafe_b64encode(struct.pack(">QQQ", 897123, 8, 9)).decode()
    )
    changed = result.model_copy(
        update={
            "content": result.content.replace(
                f'"cursor": "{cursor}"', f'"cursor": "{changed_cursor}"'
            )
        }
    )
    calls[0]["messages_json"][-1] = changed.model_dump_json()
    assert Normalizer(tmp_path).normalize(calls) != normalized


def test_storage_allowance_changes_only_outer_raw_usage() -> None:
    from yoke.agent.models import TokenUsage

    from .golden import storage_projection

    message = Message(
        role="assistant",
        content='Keep exact spacing.  {"usage":{"raw":{"scripted":true}}}',
        usage=TokenUsage(input_tokens=37, raw={"scripted": True}),
    )
    serialized = message.model_dump_json()
    calls = [{"messages_json": [serialized], "response_json": serialized}]
    normalized = storage_projection(calls)
    expected = message.model_copy(deep=True)
    assert expected.usage is not None
    expected.usage.raw = {}
    assert normalized[0]["messages_json"] == [expected.model_dump_json()]
    assert normalized[0]["response_json"] == serialized
    assert json.loads(normalized[0]["messages_json"][0])["content"] == message.content
    expected.usage.input_tokens = 38
    assert normalized != storage_projection(
        [{"messages_json": [expected.model_dump_json()], "response_json": serialized}]
    )
    expected.usage.input_tokens = 37
    expected.content = (expected.text_content() or "") + "changed"
    assert normalized != storage_projection(
        [{"messages_json": [expected.model_dump_json()], "response_json": serialized}]
    )
