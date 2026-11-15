"""Normalization must retain cursor positions and arbitrary message content."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

from yoke.agent.models import ConversationEntry, Message
from yoke.session import SessionRecord

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
            "request_context": None,
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


def test_fork_scope_cannot_alias_a_different_parent_entry(tmp_path: Path) -> None:
    normalizer = Normalizer(tmp_path)
    parent = ConversationEntry(kind="user", message=Message.user("same"))
    fork_entry = ConversationEntry(kind="user", message=Message.user("same"))
    normalizer.register(SessionRecord(id="parent", conversation_entries=[parent]))
    normalizer.register(
        SessionRecord(id="fork", conversation_entries=[fork_entry]), fork=True
    )
    calls = [
        {
            "kind": "model_iteration",
            "request_context": {"cache_scope": parent.id},
            "messages_json": [],
        }
    ]
    original = normalizer.normalize(calls)
    calls[0]["request_context"]["cache_scope"] = fork_entry.id
    assert normalizer.normalize(calls) != original
